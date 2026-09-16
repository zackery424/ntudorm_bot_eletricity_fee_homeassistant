#!/usr/bin/env python3
"""
主程式：登入太子學舍電力計費系統，抓今天的用電度數 / 帳戶餘額 / 最近一次扣款，
推送進 Home Assistant（透過 REST API + 長期存取權杖）。

用法：
    python3 ha_sync.py

設定：全部透過環境變數（可以放在同目錄的 .env，用 --env-file 或
python-dotenv 載入，也可以直接在 systemd/cron 裡設定），詳見 .env.example。

建議排程頻率：每 30~60 分鐘一次即可。來源網站的用電度數本身就是「每小時」
才更新一次、帳戶餘額/扣款金額是「每天」才更新一次，抓得更頻繁不會有更新的
資料，只會增加對學校系統的負擔。
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime, timezone

import requests

from dorm_client import DormClient, DormLoginError

logger = logging.getLogger("ha_sync")


class ConfigError(RuntimeError):
    pass


def _load_dotenv_if_present() -> None:
    """如果同目錄有 .env 檔就簡單載入（不強制依賴 python-dotenv）。"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def _get_env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise ConfigError(f"缺少必要的環境變數：{name}")
    return value


def push_state(
    ha_url: str,
    ha_token: str,
    entity_id: str,
    state,
    attributes: dict,
    timeout: float = 10.0,
) -> None:
    url = f"{ha_url.rstrip('/')}/api/states/{entity_id}"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json",
        },
        json={"state": state, "attributes": attributes},
        timeout=timeout,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"推送 {entity_id} 到 Home Assistant 失敗："
            f"HTTP {resp.status_code} {resp.text[:300]}"
        )
    logger.info("已更新 %s = %s", entity_id, state)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    _load_dotenv_if_present()

    try:
        account = _get_env("DORM_ACCOUNT", required=True)
        password = _get_env("DORM_PASSWORD", required=True)
        login_path = _get_env("DORM_LOGIN_PATH", default="/User/D02")
        ha_url = _get_env("HA_URL", required=True)
        ha_token = _get_env("HA_TOKEN", required=True)
        entity_prefix = _get_env("ENTITY_PREFIX", default="sensor.dorm_power")
        max_attempts = int(_get_env("MAX_LOGIN_ATTEMPTS", default="12"))
    except ConfigError as exc:
        logger.error("設定錯誤：%s", exc)
        logger.error("請參考 .env.example，把需要的環境變數設定好。")
        return 2

    client = DormClient(
        account=account,
        password=password,
        login_entry_path=login_path,
        max_login_attempts=max_attempts,
    )

    try:
        client.login()
    except DormLoginError as exc:
        logger.error("登入失敗：%s", exc)
        return 1

    today = date.today()

    try:
        hourly = client.get_hourly_power(today)
        account_records = client.get_account_records(today.year, today.month)
        room_no = client.get_room_no()
    except Exception:
        logger.exception("登入成功後抓資料失敗，可能是網站結構改版，請檢查。")
        return 1

    today_kwh = round(sum(h.kwh for h in hourly), 3)
    latest_hour_record = max(hourly, key=lambda h: h.hour) if hourly else None

    balance = client.get_latest_balance(account_records)
    last_charge_record = client.get_latest_settled_charge(account_records)

    now_iso = datetime.now(timezone.utc).isoformat()

    common_attrs = {}
    if room_no:
        common_attrs["room_no"] = room_no

    push_state(
        ha_url,
        ha_token,
        f"{entity_prefix}_today_kwh",
        today_kwh,
        {
            "friendly_name": "宿舍今日累計用電度數",
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
            "icon": "mdi:flash",
            **common_attrs,
        },
    )

    if latest_hour_record is not None:
        push_state(
            ha_url,
            ha_token,
            f"{entity_prefix}_latest_hour_kwh",
            latest_hour_record.kwh,
            {
                "friendly_name": "宿舍最近一小時用電度數",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "state_class": "measurement",
                "hour": latest_hour_record.hour,
                "icon": "mdi:flash-outline",
                **common_attrs,
            },
        )

    if balance is not None:
        push_state(
            ha_url,
            ha_token,
            f"{entity_prefix}_balance",
            balance,
            {
                "friendly_name": "宿舍電費帳戶餘額",
                "unit_of_measurement": "TWD",
                "device_class": "monetary",
                "icon": "mdi:cash",
                **common_attrs,
            },
        )

    if last_charge_record is not None and last_charge_record.charge is not None:
        push_state(
            ha_url,
            ha_token,
            f"{entity_prefix}_last_charge",
            last_charge_record.charge,
            {
                "friendly_name": "宿舍最近一日電費扣款",
                "unit_of_measurement": "TWD",
                "device_class": "monetary",
                "charge_date": last_charge_record.date.isoformat(),
                "icon": "mdi:cash-minus",
                **common_attrs,
            },
        )

    push_state(
        ha_url,
        ha_token,
        f"{entity_prefix}_last_sync",
        now_iso,
        {
            "friendly_name": "宿舍電費資料最後同步時間",
            "device_class": "timestamp",
            "icon": "mdi:sync",
            **common_attrs,
        },
    )

    logger.info(
        "同步完成：今日 %.2f kWh，最近一小時 %s kWh，餘額 %s 元，最近扣款 %s 元（%s）",
        today_kwh,
        latest_hour_record.kwh if latest_hour_record else "N/A",
        balance,
        last_charge_record.charge if last_charge_record else "N/A",
        last_charge_record.date if last_charge_record else "N/A",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
