"""
太子學舍電力計費雲端平台 (dormtopup.prince.com.tw) 的爬蟲用戶端。

流程：
    1. 用 requests.Session() 走一般表單登入（帳號 / 密碼 / 驗證碼）。
    2. 驗證碼用 dorm_captcha.solve_captcha() 自動辨識；辨識失敗或登入被拒
       （通常是猜錯驗證碼）就換一張新的驗證碼重試，最多 MAX_LOGIN_ATTEMPTS 次。
    3. 登入成功後，用同一個 Session 去抓：
         - 房間用電紀錄（每日 / 每小時 kWh）
         - 帳戶交易紀錄（每日扣款金額、餘額）

注意：這個平台目前看到的資料更新頻率是「每小時」（用電度數）跟「每日」
（扣款金額 / 餘額），所以排程抓取的頻率設定在 30~60 分鐘一次就足夠即時了，
抓得更頻繁也不會有更新的資料，只會增加對學校系統的負擔，請保持節制。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

from dorm_captcha import solve_captcha

logger = logging.getLogger("dorm_client")

BASE_URL = "https://dormtopup.prince.com.tw"
VALIDATE_IMG_PATH = "/User/Default/Page/ValidateImg"

# 判斷「還在登入頁」的依據：頁面裡還有 verifycode 這個欄位
_LOGIN_FORM_MARKER = "verifycode"
# 判斷「登入成功」的依據：選單裡會出現這個文字
_LOGGED_IN_MARKER = "房間用電資訊"


class DormLoginError(RuntimeError):
    """多次重試後仍然無法登入（帳密錯誤，或驗證碼一直辨識失敗/被拒）。"""


@dataclass
class DailyPower:
    date: date
    kwh: float


@dataclass
class HourlyPower:
    hour: int
    kwh: float


@dataclass
class AccountRecord:
    date: date
    charge: Optional[float]  # 當日扣款金額（元），還沒結算的當天通常是 None
    balance: Optional[float]  # 該筆紀錄之後的帳戶餘額（元）


class DormClient:
    def __init__(
        self,
        account: str,
        password: str,
        login_entry_path: str = "/User/D02",
        max_login_attempts: int = 12,
        retry_delay_seconds: float = 2.5,
        timeout: float = 15.0,
    ) -> None:
        self.account = account
        self.password = password
        self.login_entry_path = login_entry_path
        self.max_login_attempts = max_login_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.timeout = timeout

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (compatible; dorm-ha-sync/1.0; "
                    "+https://github.com/) HomeAssistantIntegration"
                )
            }
        )
        self._login_page_url: Optional[str] = None
        self._room_section_prefix: Optional[str] = None  # 例如 /User/D02

    # ------------------------------------------------------------------
    # 登入
    # ------------------------------------------------------------------

    def _fetch_login_page(self) -> str:
        resp = self.session.get(
            BASE_URL + self.login_entry_path, timeout=self.timeout
        )
        resp.raise_for_status()
        self._login_page_url = resp.url  # 表單沒有 action，會 POST 回這個網址
        return resp.text

    def _fetch_captcha_image(self) -> bytes:
        resp = self.session.get(
            BASE_URL + VALIDATE_IMG_PATH,
            timeout=self.timeout,
            headers={"Accept": "image/*"},
        )
        resp.raise_for_status()
        return resp.content

    @staticmethod
    def _looks_logged_in(html: str) -> bool:
        if _LOGIN_FORM_MARKER in html:
            return False
        return _LOGGED_IN_MARKER in html

    def login(self) -> None:
        """登入，內部會自動處理驗證碼重試。失敗會丟 DormLoginError。"""
        html = self._fetch_login_page()
        if self._looks_logged_in(html):
            logger.info("既有 session 已經是登入狀態，略過登入流程。")
            return

        last_raw_guess = None
        for attempt in range(1, self.max_login_attempts + 1):
            captcha_bytes = self._fetch_captcha_image()
            result = solve_captcha(captcha_bytes)
            if result is None:
                logger.info(
                    "第 %d 次嘗試：驗證碼辨識沒有把握（格式不符），直接換下一張。",
                    attempt,
                )
                time.sleep(self.retry_delay_seconds)
                continue

            last_raw_guess = result.raw_text
            logger.info(
                "第 %d 次嘗試：辨識到「%s」，猜測答案 = %s",
                attempt,
                result.raw_text,
                result.answer,
            )

            resp = self.session.post(
                self._login_page_url,
                data={
                    "account": self.account,
                    "password": self.password,
                    "verifycode": str(result.answer),
                },
                timeout=self.timeout,
                allow_redirects=True,
            )
            resp.raise_for_status()

            if self._looks_logged_in(resp.text):
                logger.info("登入成功（第 %d 次嘗試）。", attempt)
                return

            if _LOGIN_FORM_MARKER not in resp.text:
                # 不在登入頁也沒看到登入成功的標記，可能是帳密本身就錯了、
                # 或者頁面結構跟預期不同，直接中止比較安全。
                raise DormLoginError(
                    "登入後的頁面既不像登入頁也不像登入成功後的頁面，"
                    "請人工確認帳號密碼是否正確，或網站頁面是否改版。"
                )

            time.sleep(self.retry_delay_seconds)

        raise DormLoginError(
            f"嘗試 {self.max_login_attempts} 次後仍未能登入成功"
            f"（最後一次辨識結果：{last_raw_guess!r}）。"
            "如果帳號密碼確定正確，可能是驗證碼辨識率不夠，"
            "可以嘗試調大 max_login_attempts，或回報樣本以改善辨識邏輯。"
        )

    # ------------------------------------------------------------------
    # 房間用電資訊
    # ------------------------------------------------------------------

    def _room_prefix(self) -> str:
        # 目前登入用的 entry path 本身就是 /User/D02 這種格式，直接沿用。
        return self.login_entry_path

    def get_daily_power(self, year: int, month: int) -> List[DailyPower]:
        url = f"{BASE_URL}{self._room_prefix()}/RoomPowerRecord/List"
        resp = self.session.get(
            url,
            params={"SearchYear": year, "SearchMonth": month},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return parse_daily_power_html(resp.text)

    def get_hourly_power(self, target_date: date) -> List[HourlyPower]:
        url = f"{BASE_URL}{self._room_prefix()}/RoomPowerRecord/List"
        resp = self.session.get(
            url,
            params={
                "searchDate": target_date.strftime("%Y%m%d"),
                "SearchYear": target_date.year,
                "SearchMonth": target_date.month,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return parse_hourly_power_html(resp.text)

    def get_room_no(self) -> Optional[str]:
        url = f"{BASE_URL}{self._room_prefix()}/RoomPowerRecord/List"
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return parse_room_no_html(resp.text)

    # ------------------------------------------------------------------
    # 帳戶交易紀錄（扣款金額 / 餘額）
    # ------------------------------------------------------------------

    def get_account_records(self, year: int, month: int) -> List[AccountRecord]:
        url = f"{BASE_URL}{self._room_prefix()}/PersonalAccountRecord/List"
        resp = self.session.get(
            url,
            params={
                "SearchYear": year,
                "SearchMonth": month,
                "PrePaidAccountType": "USER",
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return parse_account_records_html(resp.text)

    def get_latest_balance(self, records: List[AccountRecord]) -> Optional[float]:
        for rec in records:
            if rec.balance is not None:
                return rec.balance
        return None

    def get_latest_settled_charge(self, records: List[AccountRecord]) -> Optional[AccountRecord]:
        for rec in records:
            if rec.charge is not None:
                return rec
        return None


def parse_daily_power_html(html: str) -> List[DailyPower]:
    soup = BeautifulSoup(html, "html.parser")
    records: List[DailyPower] = []
    for table in soup.find_all("table"):
        header = table.find("thead")
        if not header:
            continue
        htxt = header.get_text()
        if "用電度數" not in htxt or "日期時間" not in htxt:
            continue
        for row in table.find("tbody").find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all("td")]
            if len(cells) < 2:
                continue
            date_str, kwh_str = cells[0], cells[1]
            m = re.match(r"(\d{4})/(\d{2})/(\d{2})", date_str)
            if not m:
                continue
            try:
                kwh = float(kwh_str)
            except ValueError:
                continue
            y, mo, d = map(int, m.groups())
            records.append(DailyPower(date=date(y, mo, d), kwh=kwh))
        break  # 每日表永遠是頁面裡第一張符合條件的表
    return records


def parse_hourly_power_html(html: str) -> List[HourlyPower]:
    soup = BeautifulSoup(html, "html.parser")
    records: List[HourlyPower] = []
    for table in soup.find_all("table"):
        header = table.find("thead")
        if not header:
            continue
        htxt = header.get_text()
        if "用電度數" not in htxt or "日期時間" not in htxt:
            continue
        body = table.find("tbody")
        if body is None:
            continue
        rows = body.find_all("tr")
        if not rows:
            continue
        # 每日表跟每小時表結構很像，用第一列的日期欄位有沒有 "HH:00" 分辨
        sample_cell = rows[0].find_all("td")[0].get_text(strip=True)
        if not re.search(r"\d{2}:00", sample_cell):
            continue  # 這是每日表，跳過
        for row in rows:
            cells = [c.get_text(strip=True) for c in row.find_all("td")]
            if len(cells) < 2:
                continue
            m = re.search(r"(\d{2}):00", cells[0])
            if not m:
                continue
            try:
                kwh = float(cells[1])
            except ValueError:
                continue
            records.append(HourlyPower(hour=int(m.group(1)), kwh=kwh))
    return records


def parse_room_no_html(html: str) -> Optional[str]:
    m = re.search(r"房號[：:]\s*([A-Za-z0-9]+)", html)
    return m.group(1) if m else None


def parse_account_records_html(html: str) -> List[AccountRecord]:
    soup = BeautifulSoup(html, "html.parser")

    table = None
    for t in soup.find_all("table"):
        head = t.find("thead")
        if head and "餘額" in head.get_text():
            table = t
            break
    if table is None:
        return []

    records: List[AccountRecord] = []
    current_date: Optional[date] = None
    for row in table.find("tbody").find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all("td")]
        if not cells:
            continue
        m = re.match(r"(\d{4})/(\d{2})/(\d{2})", cells[0])
        if m:
            y, mo, d = map(int, m.groups())
            current_date = date(y, mo, d)
            balance_str = cells[-1] if len(cells) >= 1 else ""
            balance = _to_float_or_none(balance_str)
            # 這一行是「當天結餘」的標題行，扣款金額還沒出現在這一行
            records.append(AccountRecord(date=current_date, charge=None, balance=balance))
            continue

        # 明細行的欄位對應到表頭：紀錄時間(空) / 交易類型 / 帳戶 / 存入 / 扣除 / 餘額(空)
        if current_date is None:
            continue
        if len(cells) >= 5 and "房間用電" in cells[1]:
            charge = _to_float_or_none(cells[4])
            if records and records[-1].date == current_date:
                records[-1].charge = charge
    return records


def _to_float_or_none(text: str) -> Optional[float]:
    text = text.strip().replace(",", "")
    if not text or text in ("---", "-", "—"):
        return None
    try:
        return float(text)
    except ValueError:
        return None
