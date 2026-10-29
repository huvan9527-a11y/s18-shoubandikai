import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
from src import data

class FinalBars(unittest.TestCase):
    def test_only_verified_final_quote_supplements_missing_day(self):
        day=dt.date(2026,10,29)
        code='600001.XSHG'
        class Clock(dt.datetime):
            @classmethod
            def now(cls,tz=None):return cls(2026,10,29,16,50,tzinfo=tz)
        for stamp,previous,expected in [('20261029150000','10',True),('20261028150000','10',False),('20261029145959','10',False),('20261029150000','9.8',False)]:
            with self.subTest(stamp=stamp,previous=previous),tempfile.TemporaryDirectory() as tmp:
                p=data.PublicData(Path(tmp)); p.frames[code]=pd.DataFrame([dict(time=pd.Timestamp('2026-10-28'),close=10.)])
                f=['0']*49
                for i,v in {3:'10.5',4:previous,5:'10.1',6:'100',30:stamp,33:'10.7',34:'10',37:'100',47:'11',48:'9'}.items():f[i]=v
                response=SimpleNamespace(content=('v_sh600001="'+'~'.join(f)+'";').encode('gbk'))
                with patch.object(data.dt,'datetime',Clock),patch.object(data,'get',return_value=response):
                    p.refresh_final_bars([code],day)
                self.assertEqual(len(p.frames[code]),2 if expected else 1)
                if expected:
                    self.assertEqual(p.frames[code].iloc[-1].money,1000000)
                    self.assertEqual(p.frames[code].iloc[-1].close,10.5)
