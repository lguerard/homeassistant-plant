"""Watering schedule and adaptation logic for plant integration.

This module provides functions to compute the next watering datetime based on:
- last watered datetime
- base interval (from plant DB)
- ambient temperature and humidity
- outdoor adjustment (based on weather)

Formulas are intentionally conservative and well-tested; exact multipliers are TODO and can be tuned by users.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

# Small safety bounds
MIN_INTERVAL_DAYS = 0.5
MAX_INTERVAL_DAYS = 365


def _temp_modifier(temp_c: Optional[float]) -> float:
    """Return a multiplier for interval based on temperature in °C.

    Higher temperatures -> faster drying -> shorter interval (multiplier < 1).
    Lower temperatures -> slower drying -> longer interval (multiplier > 1).

    This is a simple linear mapping with clamping; replace with a more
    sophisticated formula if desired.
    """
    if temp_c is None:
        return 1.0
    # Base: 20°C gives neutral modifier 1.0
    diff = temp_c - 20.0
    # Each °C above reduces interval by 2% (multiplier 0.98), each °C below increases by 2%
    modifier = 1.0 - (diff * 0.02)
    return max(0.5, min(2.0, modifier))


def _humidity_modifier(hum_pct: Optional[float]) -> float:
    """Return a multiplier for interval based on relative humidity (%).

    Higher humidity -> slower drying -> longer interval (multiplier > 1).
    Lower humidity -> faster drying -> shorter interval (multiplier < 1).
    """
    if hum_pct is None:
        return 1.0
    # Neutral at 50%
    diff = hum_pct - 50.0
    # Each 10% above increases interval by 5%
    modifier = 1.0 + (diff / 10.0) * 0.05
    return max(0.5, min(2.0, modifier))


def _dli_modifier(dli: Optional[float]) -> float:
    """Return a multiplier for interval based on Daily Light Integral (DLI).

    Higher DLI -> more photosynthesis/transpiration -> shorter interval.
    Neutral around 5.0 mol/d·m² for indoor plants.
    """
    if dli is None:
        return 1.0
    # Higher DLI reduces interval. 0 -> 1.5x, 5 -> 1.0x, 20 -> 0.5x
    # We use a non-linear approach as very high light accelerates drying exponentially
    if dli < 5.0:
        modifier = 1.0 + (5.0 - dli) * 0.1  # Max 1.5 at 0 DLI
    else:
        modifier = 1.0 - (dli - 5.0) * 0.05  # Min 0.25 at 20 DLI
    return max(0.25, min(1.5, modifier))


def _seasonal_modifier() -> float:
    """Return a multiplier based on the day of the year.

    Interval stays standard in Spring/Fall, shorter in Summer, longer in Winter.
    Helps for plants without accurate light sensors.
    """
    from math import cos, pi

    day_of_year = datetime.utcnow().timetuple().tm_yday
    # Peak summer at day 172. Peak winter at day 355.
    # Returns 0.8 in Summer (peak) and 1.2 in Winter (peak)
    cos_val = cos(2 * pi * (day_of_year - 172) / 365.25)
    return 1.0 + (cos_val * 0.2)


def _outdoor_modifier(is_outside: bool, weather_dryness: Optional[float]) -> float:
    """Return modifier for outdoor placement.

    If outside, we use `weather_dryness` (0.0..1.0) where 1.0 is very dry -> much shorter interval.
    If `weather_dryness` is None, a conservative outdoors modifier is applied.
    """
    if not is_outside:
        return 1.0
    if weather_dryness is None:
        return 0.8
    # Blend between 0.6 (very dry) and 1.2 (very wet)
    return max(0.4, min(1.5, 1.2 - (weather_dryness - 0.5)))


def _clamp_interval(days: float) -> float:
    return max(MIN_INTERVAL_DAYS, min(MAX_INTERVAL_DAYS, days))


def weather_dryness_from_attrs(attrs: dict) -> Optional[float]:
    """Estimate a dryness index (0.0..1.0) from weather entity attributes.

    The function uses precipitation probability or condition heuristics to
    return a float where 1.0 is very dry and 0.0 is very wet. Returns None if
    no suitable data is found.
    """
    if not attrs:
        return None

    # Check forecast if available (provides proactive estimation for next 24-48h)
    forecast = attrs.get("forecast")
    if isinstance(forecast, list) and len(forecast) > 0:
        dryness_sum = 0.0
        count = 0
        # Blend current state with next 2 forecast periods
        for entry in forecast[:2]:
            val = weather_dryness_from_attrs(entry)
            if val is not None:
                dryness_sum += val
                count += 1
        if count > 0:
            # Recursive base case avoids infinite loop because 'forecast' won't be in 'entry'
            return dryness_sum / count

    # Try precipitation probability fields
    for key in ("precipitation_probability", "precip_prob", "precipitationProbability"):
        if key in attrs:
            try:
                prob = float(attrs[key])
                prob = max(0.0, min(100.0, prob))
                return max(0.0, min(1.0, 1.0 - prob / 100.0))
            except (ValueError, TypeError):
                continue
    # Try precipitation amount (higher -> wetter)
    for key in ("precipitation", "precipitation_amount"):
        if key in attrs:
            try:
                amount = float(attrs[key])
                # Very naive mapping: 0 -> dry (1.0), high amounts -> wet (0.0)
                return max(0.0, min(1.0, 1.0 - min(50.0, amount) / 50.0))
            except (ValueError, TypeError):
                continue
    # Check condition text as heuristic
    condition = (
        attrs.get("condition") if isinstance(attrs.get("condition"), str) else None
    )
    if condition:
        cond = condition.lower()
        if cond in ("clear", "sunny", "partlycloudy", "mostly_sunny"):
            return 0.9
        if cond in ("cloudy", "partly_cloudy", "mostly_cloudy"):
            return 0.6
        if cond in ("rain", "rainy", "snow", "sleet", "thunderstorm"):
            return 0.1
    # No useful info
    return None


def next_watering(
    last_watered: datetime,
    base_interval_days: float,
    temperature_c: Optional[float] = None,
    humidity_pct: Optional[float] = None,
    is_outside: bool = False,
    weather_dryness: Optional[float] = None,
    dli: Optional[float] = None,
) -> tuple[datetime, str]:
    """Compute next watering datetime and return explanation.

    Arguments:
    last_watered: datetime
        When the plant was last watered.
    base_interval_days: float
        Base interval in days.
    temperature_c: Optional[float]
        Ambient temperature in °C.
    humidity_pct: Optional[float]
        Ambient relative humidity in %.
    is_outside: bool
        Whether the plant is outside.
    weather_dryness: Optional[float]
        A dryness index from 0.0 (wet) to 1.0 (very dry) to factor outdoor impact.
    dli: Optional[float]
        Daily Light Integral.

    Returns
    -------
    tuple[datetime, str]
        When next watering is due and a human-friendly explanation.
    """
    if base_interval_days is None:
        base_interval_days = 7.0

    temp_mod = _temp_modifier(temperature_c)
    hum_mod = _humidity_modifier(humidity_pct)
    out_mod = _outdoor_modifier(is_outside, weather_dryness)
    dli_mod = _dli_modifier(dli)
    season_mod = _seasonal_modifier()

    combined = temp_mod * hum_mod * out_mod * dli_mod * season_mod
    interval = base_interval_days * combined
    interval = _clamp_interval(interval)

    explanation_parts = []
    if temp_mod > 1.05:
        explanation_parts.append("cool temperature")
    elif temp_mod < 0.95:
        explanation_parts.append("warm temperature")

    if hum_mod > 1.05:
        explanation_parts.append("high humidity")
    elif hum_mod < 0.95:
        explanation_parts.append("dry air")

    if season_mod > 1.1:
        explanation_parts.append("winter dormancy")
    elif season_mod < 0.9:
        explanation_parts.append("summer growth")

    if is_outside:
        if out_mod < 0.8:
            explanation_parts.append("dry weather")
        elif out_mod > 1.2:
            explanation_parts.append("wet weather")

    if dli_mod < 0.9:
        explanation_parts.append("lots of light")
    elif dli_mod > 1.1:
        explanation_parts.append("low light")

    if not explanation_parts:
        explanation = "Ideal conditions"
    else:
        explanation = f"Adjusted for {', '.join(explanation_parts)}"

    return last_watered + timedelta(days=interval), explanation


# Additional helper: compute days until watering (float days)
def days_until(next_dt: datetime, from_dt: Optional[datetime] = None) -> float:
    if from_dt is None:
        from_dt = datetime.utcnow()
    delta = next_dt - from_dt
    return delta.total_seconds() / 86400.0


def get_weather_dryness(hass, weather_entity: Optional[str] = None) -> Optional[float]:
    """Fetch weather entity and estimate dryness.

    Parameters
    ----------
    hass : HomeAssistant
        Home Assistant instance.
    weather_entity : Optional[str]
        Optional weather entity id to query. If omitted, attempts to use
        first available weather entity.
    """
    try:
        from homeassistant.core import HomeAssistant  # local import for type hints
    except Exception:  # pragma: no cover - type hint convenience
        HomeAssistant = object

    if weather_entity:
        st = hass.states.get(weather_entity)
        if not st:
            return None
        return weather_dryness_from_attrs(st.attributes)

    # Fallback: try to find any weather entity
    for state in hass.states.async_all("weather"):
        if state and state.attributes:
            val = weather_dryness_from_attrs(state.attributes)
            if val is not None:
                return val
    return None
