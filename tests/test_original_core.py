"""Offline invariants for the recovered downloader. Never contacts IBKR."""
from dataclasses import dataclass
from datetime import datetime,timezone
import pandas as pd
import pytest
from ibkr_history_core import (
    Settings,build_download_plan,contract_folder,filter_eligible_plan,
    is_regular_globex_minute_chicago,parse_utc,
)

@dataclass
class Contract:
    symbol: str="NQ"
    localSymbol: str="NQZ6"
    conId: int=563947726
    lastTradeDateOrContractMonth: str="20261218"
    secType: str="FUT"

def test_original_tws_default_configuration():
    s=Settings()
    assert (s.host,s.port)==("127.0.0.1",7496)
    assert s.coverage_mode=="roll_window" and s.smoke_test
    s.validate()

def test_original_futures_contract_folder_unchanged(tmp_path):
    assert contract_folder(tmp_path,Contract())==tmp_path/"raw"/"NQ"/"NQZ6_563947726"

def test_roll_window_plan_does_not_stitch_contract_prices():
    s=Settings(symbols=("NQ",),stocks=())
    now=datetime(2026,10,23,12,tzinfo=timezone.utc)
    plan=build_download_plan([Contract()],s,now=now)
    assert len(plan)==1
    assert plan.iloc[0].start_utc==datetime(2026,9,4,tzinfo=timezone.utc)
    active,inactive=filter_eligible_plan(plan)
    assert len(active)==1 and inactive.empty

def test_naive_timestamp_fails_closed():
    with pytest.raises(ValueError,match="Naive"):
        parse_utc("2026-10-23T12:00:00")

def test_regular_globex_boundary_weekend_and_maintenance():
    cases=[("2026-10-19 16:59",True),("2026-10-19 17:00",False),
           ("2026-10-19 18:00",True),("2026-10-23 18:00",False),
           ("2026-10-18 18:00",True)]
    for local,opened in cases:
        stamp=pd.Timestamp(local,tz="America/New_York").tz_convert("UTC")
        assert is_regular_globex_minute_chicago(stamp) is opened
