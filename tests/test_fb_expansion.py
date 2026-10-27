import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from src import data
from src.paper import DEFAULTS

class FBUniverseExpansion(unittest.TestCase):
    def test_cached_universe_adds_only_nonst_mainboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=data.PublicData(tmp)
            rows=[dict(code=prefix+str(i).zfill(3),name='demo',symbol='sh'+prefix+str(i).zfill(3))
                  for prefix in ['600','601','603'] for i in range(1000)]
            rows += [dict(code='300001',name='demo'),dict(code='688001',name='demo'),
                     dict(code='002001',name='ST demo'),dict(code='002002',name='退市demo')]
            path=Path(tmp)/'state/metadata'/f'fb-mainboard-{p.today}.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(rows))
            pool=p.fb_mainboard()
            self.assertIn('603999.XSHG',pool)
            for code in ['300001.XSHE','688001.XSHG','002001.XSHE','002002.XSHE']:
                self.assertNotIn(code,pool)

    def test_extra_flags_cannot_create_lb_or_deep_or_change_temperature(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=data.PublicData(tmp);p.pool={'600001.XSHG':dict(name='base')}
            p.names={'600001.XSHG':'base'};p._fb_history_loaded=True
            code='603999.XSHG'
            day=dt.date(2026,10,27)
            p.listed={code:dt.date(2000,1,1)}
            p.frames={code:pd.DataFrame(dict(time=[pd.Timestamp(day)],close=[10.],open=[9.],
                high_limit=[10.],low_limit=[8.],factor=[1.]))}
            flags=pd.DataFrame(dict(fb=[True],lb=[True],gd=[True],close_t=[10.],money=[2e8]),index=[code])
            temp=dict(FB_OK=True,LB_OK=True,DEEP=True,ICE=True,RUN=2,MA20=-.1,M_DN=.01)
            original=copy.deepcopy(temp)
            with patch.object(p,'fb_mainboard',return_value={code:dict(name='extra')}),patch.object(data.signals,'stock_flags',return_value=flags):
                out=p.additional_fb_candidates(day,temp,DEFAULTS)
            self.assertEqual([x['component'] for x in out],['FB_dip'])
            self.assertEqual(temp,original)
            self.assertEqual(set(p.pool),{'600001.XSHG'})
            flags['fb']=False
            with patch.object(p,'fb_mainboard',return_value={code:dict(name='extra')}),patch.object(data.signals,'stock_flags',return_value=flags):
                self.assertEqual(p.additional_fb_candidates(day,temp,DEFAULTS),[])

    def test_fb_market_gate_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=data.PublicData(tmp);p.pool={};p.names={};p._fb_history_loaded=True
            code='603999.XSHG';day=dt.date(2026,10,27)
            p.listed={code:dt.date(2000,1,1)}
            p.frames={code:pd.DataFrame(dict(time=[pd.Timestamp(day)],close=[10.],open=[9.],
                high_limit=[10.],low_limit=[8.],factor=[1.]))}
            flags=pd.DataFrame(dict(fb=[True],lb=[True],gd=[True],close_t=[10.],money=[2e8]),index=[code])
            with patch.object(p,'fb_mainboard',return_value={code:dict(name='extra')}),patch.object(data.signals,'stock_flags',return_value=flags):
                self.assertEqual(p.additional_fb_candidates(day,dict(FB_OK=False,LB_OK=True,DEEP=True),DEFAULTS),[])

    def test_missing_date_is_isolated_like_original_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=data.PublicData(tmp);p.pool={};p.names={};p._fb_history_loaded=True
            code='605258.XSHG';day=dt.date(2026,10,27)
            p.listed={code:dt.date(2000,1,1)}
            p.frames={code:pd.DataFrame(dict(time=[pd.Timestamp('2026-10-23')],close=[10.]))}
            with patch.object(p,'fb_mainboard',return_value={code:dict(name='demo')}):
                self.assertEqual(p.additional_fb_candidates(day,dict(FB_OK=True),DEFAULTS),[])
            self.assertIn(code,p.fb_omitted)
