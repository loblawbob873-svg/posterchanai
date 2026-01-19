"""
Date utility functions for calendar and time operations.
"""
import calendar
from datetime import datetime


def get_month_end(year: int, month: int) -> datetime:
    """
    Get the last day of a month at 23:59:59.
    
    Args:
        year: The year (e.g., 2026)
        month: The month (1-12)
    
    Returns:
        A datetime object representing the last moment of the specified month.
    
    Example:
        >>> get_month_end(2026, 2)
        datetime.datetime(2026, 2, 28, 23, 59, 59)
    """
    last_day = calendar.monthrange(year, month)[1]
    return datetime(year, month, last_day, 23, 59, 59)


def get_month_start(year: int, month: int) -> datetime:
    """
    Get the first day of a month at 00:00:00.
    
    Args:
        year: The year (e.g., 2026)
        month: The month (1-12)
    
    Returns:
        A datetime object representing the first moment of the specified month.
    
    Example:
        >>> get_month_start(2026, 2)
        datetime.datetime(2026, 2, 1, 0, 0, 0)
    """
    return datetime(year, month, 1, 0, 0, 0)


def get_next_month(year: int, month: int) -> tuple[int, int]:
    """
    Get the year and month for the next month, handling year rollover.
    
    Args:
        year: The current year
        month: The current month (1-12)
    
    Returns:
        A tuple of (next_year, next_month)
    
    Example:
        >>> get_next_month(2026, 12)
        (2027, 1)
        >>> get_next_month(2026, 1)
        (2026, 2)
    """
    if month == 12:
        return (year + 1, 1)
    else:
        return (year, month + 1)
