#!/usr/bin/env python3
"""Poll domestic gold price (极速数据) and notify when it enters a threshold band."""

from __future__ import annotations

import html
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

# 极速数据：https://www.jisuapi.com/api/gold/
JISU_ENDPOINTS = {
    "shgold": "https://api.jisuapi.com/gold/shgold",  # 上海黄金交易所（默认）
    "bank": "https://api.jisuapi.com/gold/bank",  # 银行账户金
    "london": "https://api.jisuapi.com/gold/london",  # 伦敦金
    "shfutures": "https://api.jisuapi.com/gold/shfutures",  # 上海期货
    "hkgold": "https://api.jisuapi.com/gold/hkgold",  # 香港黄金
}

STATE_PATH = Path(os.environ.get("STATE_PATH", "state/last_alert.json"))
COOLDOWN_HOURS = float(os.environ.get("COOLDOWN_HOURS", "3"))
GOLD_MARKET = os.environ.get("GOLD_MARKET", "shgold").strip().lower()
# 默认盯上金所 Au99.99；也可用 Au9999 / 黄金延期 / Au(T+D) 等
GOLD_SYMBOL = os.environ.get("GOLD_SYMBOL", "Au99.99").strip()
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "20"))


class PriceFetchError(RuntimeError):
    pass


def env_float(name: str, default: float | None = None) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return float(raw)


def http_get_json(url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "gold-price-alert/1.0 (+github-actions)",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise PriceFetchError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise PriceFetchError(f"Request failed for {url}: {exc}") from exc
    return json.loads(body)


def _maybe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _norm(text: str) -> str:
    return "".join(text.lower().split())


def pick_symbol(items: list[dict[str, Any]], symbol: str) -> dict[str, Any]:
    if not items:
        raise PriceFetchError("API returned empty result list")

    want = _norm(symbol)
    # 精确匹配 type / typename
    for item in items:
        type_code = _norm(str(item.get("type") or ""))
        type_name = _norm(str(item.get("typename") or item.get("name") or ""))
        if want in {type_code, type_name}:
            return item

    # 包含匹配（如 Au9999 匹配 Au99.99 需用户写准；这里允许 Au99 / 黄金延期 等）
    for item in items:
        type_code = _norm(str(item.get("type") or ""))
        type_name = _norm(str(item.get("typename") or item.get("name") or ""))
        if want and (want in type_code or want in type_name):
            return item

    available = ", ".join(
        f"{i.get('type')}/{i.get('typename') or i.get('name')}" for i in items[:20]
    )
    raise PriceFetchError(
        f"Symbol {symbol!r} not found in {GOLD_MARKET}. Available: {available}"
    )


def fetch_jisu_price() -> dict[str, Any]:
    appkey = os.environ.get("JISU_APPKEY", "").strip()
    if not appkey:
        raise PriceFetchError(
            "Missing JISU_APPKEY. Register at https://www.jisuapi.com/ and "
            "add it as a GitHub Actions secret."
        )

    if GOLD_MARKET not in JISU_ENDPOINTS:
        raise PriceFetchError(
            f"Unsupported GOLD_MARKET={GOLD_MARKET!r}. "
            f"Use one of: {', '.join(sorted(JISU_ENDPOINTS))}"
        )

    base = JISU_ENDPOINTS[GOLD_MARKET]
    url = f"{base}?{urllib.parse.urlencode({'appkey': appkey})}"
    data = http_get_json(url)

    status = data.get("status")
    if status not in (0, "0"):
        raise PriceFetchError(
            f"Jisu API error status={status}: {data.get('msg') or data}"
        )

    result = data.get("result")
    if isinstance(result, dict):
        # 少数接口可能返回 dict
        items = list(result.values()) if result else []
        if items and not isinstance(items[0], dict):
            items = [result]
    elif isinstance(result, list):
        items = result
    else:
        raise PriceFetchError(f"Unexpected result shape: {type(result)}")

    item = pick_symbol(items, GOLD_SYMBOL)
    price = _maybe_float(item.get("price") or item.get("latestprice"))
    if price is None:
        raise PriceFetchError(f"No price field in item: {item}")

    buy = _maybe_float(item.get("buyprice") or item.get("buy"))
    sell = _maybe_float(item.get("sellprice") or item.get("sell"))

    return {
        "source": f"jisuapi/{GOLD_MARKET}",
        "market": GOLD_MARKET,
        "symbol": str(item.get("type") or GOLD_SYMBOL),
        "symbol_name": str(item.get("typename") or item.get("name") or ""),
        "price": price,
        "unit": "CNY/g",
        "buy": buy,
        "sell": sell,
        "changepercent": item.get("changepercent"),
        "updated_at": item.get("updatetime") or item.get("time"),
        "raw_item": item,
    }


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def decide_side(price: float, low: float | None, high: float | None) -> str | None:
    if low is not None and price <= low:
        return "low"
    if high is not None and price >= high:
        return "high"
    return None


def cooldown_active(state: dict[str, Any], side: str) -> bool:
    last_side = state.get("last_side")
    last_ts = state.get("last_alert_ts")
    if last_side != side or not last_ts:
        return False
    elapsed = time.time() - float(last_ts)
    return elapsed < COOLDOWN_HOURS * 3600


def notify(alert: dict[str, str]) -> list[str]:
    sent: list[str] = []
    title = alert["title"]

    wecom = os.environ.get("WECOM_WEBHOOK", "").strip()
    if wecom:
        send_wecom(wecom, title, alert["markdown"])
        sent.append("wecom")

    pushplus = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if pushplus:
        send_pushplus(pushplus, title, alert["html"])
        sent.append("pushplus")

    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_pass = os.environ.get("SMTP_PASS", "").strip()
    smtp_to = os.environ.get("SMTP_TO", "").strip() or smtp_user
    if smtp_host and smtp_user and smtp_pass and smtp_to:
        send_email(
            smtp_host,
            smtp_user,
            smtp_pass,
            smtp_to,
            title,
            alert["text"],
            alert["html"],
        )
        sent.append("email")

    if not sent:
        raise RuntimeError(
            "No notification channel configured. "
            "Set WECOM_WEBHOOK and/or PUSHPLUS_TOKEN and/or SMTP_* secrets."
        )
    return sent


def send_wecom(webhook: str, title: str, markdown: str) -> None:
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": f"### {title}\n{markdown}",
        },
    }
    post_json(webhook, payload)


def send_pushplus(token: str, title: str, html_body: str) -> None:
    payload = {
        "token": token,
        "title": title,
        "content": html_body,
        "template": "html",
    }
    post_json("https://www.pushplus.plus/send", payload)


def send_email(
    host: str,
    user: str,
    password: str,
    to_addr: str,
    title: str,
    text_body: str,
    html_body: str,
) -> None:
    port = int(os.environ.get("SMTP_PORT", "465"))
    msg = MIMEMultipart("alternative")
    msg["Subject"] = title
    msg["From"] = user
    msg["To"] = to_addr
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.sendmail(user, [to_addr], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls(context=context)
            smtp.login(user, password)
            smtp.sendmail(user, [to_addr], msg.as_string())


def post_json(url: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "gold-price-alert/1.0 (+github-actions)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Notify HTTP {exc.code} from {url}: {detail}") from exc

    if not resp_body:
        return
    try:
        data = json.loads(resp_body)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    if "errcode" in data and data.get("errcode") not in (0, "0", None):
        raise RuntimeError(f"WeCom error: {data}")
    if "code" in data and data.get("code") not in (200, "200", 0, "0", None):
        if data.get("code") != 200:
            raise RuntimeError(f"PushPlus error: {data}")


def build_alert(
    *,
    side: str,
    quote: dict[str, Any],
    low: float | None,
    high: float | None,
) -> dict[str, str]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    is_low = side == "low"
    direction = "跌破低价" if is_low else "突破高价"
    symbol_label = quote["symbol_name"] or quote["symbol"]
    price_text = f"{quote['price']:.2f}"
    unit = quote["unit"]
    title = f"金价提醒：{symbol_label} {direction}（{price_text} {unit}）"

    rows: list[tuple[str, str]] = [
        ("方向", direction),
        ("品种", f"{quote['symbol']} / {symbol_label}"),
        ("当前价", f"{price_text} {unit}"),
        ("市场", str(quote["market"])),
        ("数据源", str(quote["source"])),
        ("上游更新", str(quote.get("updated_at") or "n/a")),
        ("检查时间", now),
    ]
    if low is not None:
        rows.append(("低价阈值", f"{low:.2f} {unit}"))
    if high is not None:
        rows.append(("高价阈值", f"{high:.2f} {unit}"))
    if quote.get("buy") is not None and quote.get("sell") is not None:
        rows.append(("买 / 卖", f"{quote['buy']:.2f} / {quote['sell']:.2f}"))
    if quote.get("changepercent"):
        rows.append(("涨跌幅", str(quote["changepercent"])))

    markdown = "\n".join(f"**{k}**: {v}" for k, v in rows)
    text = "\n".join(f"{k}: {v}" for k, v in rows)
    html_body = render_email_html(
        title=title,
        direction=direction,
        is_low=is_low,
        symbol_label=symbol_label,
        price_text=price_text,
        unit=unit,
        rows=rows,
    )
    return {
        "title": title,
        "markdown": markdown,
        "text": text,
        "html": html_body,
    }


def render_email_html(
    *,
    title: str,
    direction: str,
    is_low: bool,
    symbol_label: str,
    price_text: str,
    unit: str,
    rows: list[tuple[str, str]],
) -> str:
    # Email-safe inline styles + table layout for 163/QQ clients.
    accent = "#0f766e" if is_low else "#b45309"
    badge_bg = "#ecfdf5" if is_low else "#fff7ed"
    badge_fg = "#047857" if is_low else "#c2410c"
    tip = "可能适合关注买入机会" if is_low else "可能适合关注卖出/止盈机会"

    detail_rows = []
    for label, value in rows:
        detail_rows.append(
            "<tr>"
            f'<td style="padding:10px 0;border-bottom:1px solid #eee;'
            f'color:#6b7280;font-size:13px;width:96px;">{html.escape(label)}</td>'
            f'<td style="padding:10px 0;border-bottom:1px solid #eee;'
            f'color:#111827;font-size:14px;font-weight:600;text-align:right;">'
            f"{html.escape(value)}</td>"
            "</tr>"
        )

    return f"""\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{html.escape(title)}</title>
</head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 8px 24px rgba(15,23,42,0.08);">
          <tr>
            <td style="background:linear-gradient(135deg,{accent} 0%,#111827 100%);padding:28px 28px 22px 28px;color:#ffffff;">
              <div style="font-size:12px;letter-spacing:0.08em;opacity:0.85;text-transform:uppercase;">GOLD PRICE ALERT</div>
              <div style="font-size:22px;font-weight:700;margin-top:8px;line-height:1.35;">{html.escape(symbol_label)} · {html.escape(direction)}</div>
              <div style="margin-top:14px;">
                <span style="display:inline-block;background:{badge_bg};color:{badge_fg};border-radius:999px;padding:6px 12px;font-size:12px;font-weight:700;">
                  {html.escape(direction)}
                </span>
              </div>
            </td>
          </tr>
          <tr>
            <td style="padding:28px;">
              <div style="text-align:center;padding:8px 0 20px 0;">
                <div style="color:#6b7280;font-size:13px;margin-bottom:6px;">当前价格</div>
                <div style="font-size:40px;line-height:1.1;font-weight:800;color:#111827;letter-spacing:-0.02em;">
                  {html.escape(price_text)}
                  <span style="font-size:16px;font-weight:600;color:#6b7280;margin-left:4px;">{html.escape(unit)}</span>
                </div>
                <div style="margin-top:10px;color:#9ca3af;font-size:12px;">{html.escape(tip)}</div>
              </div>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px;">
                {"".join(detail_rows)}
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 28px 24px 28px;background:#fafafa;color:#9ca3af;font-size:12px;line-height:1.6;">
              本邮件由 gold-price GitHub Actions 自动发送，仅供参考，不构成投资建议。
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


def main() -> int:
    low = env_float("GOLD_LOW")
    high = env_float("GOLD_HIGH")
    if low is None and high is None:
        raise SystemExit("Set at least one of GOLD_LOW / GOLD_HIGH (CNY/g for shgold)")

    if low is not None and high is not None and low > high:
        raise SystemExit("GOLD_LOW must be <= GOLD_HIGH")

    quote = fetch_jisu_price()
    print(
        f"Fetched {quote['symbol']} ({quote['symbol_name']}) "
        f"{quote['price']:.2f} {quote['unit']} via {quote['source']}"
    )

    side = decide_side(quote["price"], low, high)
    state = load_state()
    state.update(
        {
            "last_price": quote["price"],
            "last_unit": quote["unit"],
            "last_symbol": quote["symbol"],
            "last_market": quote["market"],
            "last_source": quote["source"],
            "last_check_ts": time.time(),
            "last_check_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    if side is None:
        print("Price is within band; no alert.")
        save_state(state)
        return 0

    if cooldown_active(state, side):
        print(
            f"Alert suppressed by cooldown "
            f"({COOLDOWN_HOURS}h, last_side={state.get('last_side')})."
        )
        save_state(state)
        return 0

    alert = build_alert(side=side, quote=quote, low=low, high=high)
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() in {"1", "true", "yes"}
    if dry_run:
        print("DRY_RUN=true; skip notify. Would send:")
        print(f"TITLE: {alert['title']}")
        print(alert["text"])
        preview = Path("state/email_preview.html")
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_text(alert["html"], encoding="utf-8")
        print(f"HTML preview written to {preview}")
        save_state(state)
        return 0

    channels = notify(alert)
    print(f"Alert sent via: {', '.join(channels)}")

    state["last_side"] = side
    state["last_alert_ts"] = time.time()
    state["last_alert_at"] = datetime.now(timezone.utc).isoformat()
    state["last_alert_title"] = alert["title"]
    save_state(state)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
