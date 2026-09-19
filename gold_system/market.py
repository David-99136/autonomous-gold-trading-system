"""把券商規格轉成程式可檢查的限制；百分比距離每次依價格重算。"""
from dataclasses import dataclass
from datetime import datetime, timedelta, time, timezone
from decimal import ROUND_CEILING
from zoneinfo import ZoneInfo
from .core import Contract, D, Rejected


def decimal(value):
    result = D(str(value))
    if not result.is_finite():
        raise Rejected("INVALID_MARKET_NUMBER")
    return result


@dataclass(frozen=True)
class MarketRules:
    payload: dict
    fetched_at: datetime

    def __post_init__(self):
        instrument = self.payload["instrument"]
        if (instrument.get("epic") != "GOLD" or instrument.get("type") != "COMMODITIES"
                or instrument.get("expiry") != "-" or instrument.get("currency") != "USD"):
            raise Rejected("NOT_GOLD_SPOT_USD")
        if self.fetched_at.tzinfo is None:
            raise Rejected("MARKET_TIMESTAMP_REQUIRED")

    @property
    def tick(self):
        # minStepDistance 是價格步進；size increment 是另一個欄位。
        step = self.payload["dealingRules"]["minStepDistance"]
        if step["unit"] != "POINTS":
            raise Rejected("UNSUPPORTED_PRICE_STEP_UNIT")
        value = decimal(step["value"])
        if value <= 0:
            raise Rejected("INVALID_PRICE_STEP")
        return value

    def distance(self, name, price):
        rule = self.payload["dealingRules"][name]
        value = decimal(rule["value"])
        if value < 0:
            raise Rejected("INVALID_DISTANCE")
        if rule["unit"] == "PERCENTAGE":
            value = price * value / 100
        elif rule["unit"] != "POINTS":
            raise Rejected("UNSUPPORTED_DISTANCE_UNIT")
        return (value / self.tick).to_integral_value(rounding=ROUND_CEILING) * self.tick

    def contract(self, price, verified_value_per_point):
        """點值需有已核對來源，不能把 lotSize 自動視為每美元盈虧乘數。"""
        if verified_value_per_point is None:
            raise Rejected("POINT_VALUE_NOT_VERIFIED")
        instrument, rules = self.payload["instrument"], self.payload["dealingRules"]
        if instrument["marginFactorUnit"] != "PERCENTAGE":
            raise Rejected("UNSUPPORTED_MARGIN_UNIT")
        return Contract("GOLD", decimal(verified_value_per_point), decimal(rules["minDealSize"]["value"]),
                        decimal(rules["minSizeIncrement"]["value"]), decimal(rules["maxDealSize"]["value"]),
                        decimal(instrument["marginFactor"]) / 100,
                        self.distance("minStopOrProfitDistance", price))

    def sessions(self, now):
        hours = self.payload["instrument"]["openingHours"]
        tz = timezone.utc if hours["zone"] == "UTC" else ZoneInfo(hours["zone"])
        local = now.astimezone(tz)
        weekdays = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        sessions = []
        for delta in range(-1, 8):
            day = local.date() + timedelta(days=delta)
            for entry in hours[weekdays[day.weekday()]]:
                start, end = entry.split(" - ")
                start_dt = datetime.combine(day, time.fromisoformat(start), tzinfo=tz)
                end_dt = datetime.combine(day, time.fromisoformat(end), tzinfo=tz)
                if end_dt <= start_dt:
                    end_dt += timedelta(days=1)
                sessions.append((start_dt.astimezone(timezone.utc), end_dt.astimezone(timezone.utc)))
        # 券商常把午夜前後拆成兩段；合併相接區間避免午夜被誤認成休市。
        merged = []
        for start, end in sorted(sessions):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    def boundary(self, now):
        if now.tzinfo is None or not timedelta(0) <= now - self.fetched_at <= timedelta(minutes=5):
            raise Rejected("MARKET_RULES_STALE")
        current = next(((s, e) for s, e in self.sessions(now) if s <= now < e), None)
        if current is None:
            return None
        fee = self.payload["instrument"]["overnightFee"]
        funding = datetime.fromtimestamp(float(decimal(fee["swapChargeTimestamp"]) / 1000), timezone.utc)
        # 不自行把過期 funding timestamp 往後推一天，需重新讀券商。
        if funding <= now:
            raise Rejected("FUNDING_TIMESTAMP_STALE")
        return min(current[1], funding)

    def entry_status(self, now):
        boundary = self.boundary(now)
        snapshot = self.payload["snapshot"]
        return bool(boundary and snapshot.get("marketStatus") == "TRADEABLE"
                    and snapshot.get("marketModes") == ["REGULAR"]
                    and boundary - now > timedelta(minutes=30))
