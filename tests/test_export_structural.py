import io
import json
import unittest
from contextlib import redirect_stdout
import export_tab_records as e

def record(id): return {'id':id,'cleartext':{'id':id,'kind':'tab','data':{}}}

class ExportReportTests(unittest.TestCase):
    def report(self,ids,records,structural,**kw):
        out=io.StringIO()
        with redirect_stdout(out):
            code=e.report(ids,{'records':records,'structural':structural},**kw)
        return code,json.loads(out.getvalue())
    def test_zen_empty_tabs_do_not_fail_the_export(self):
        # Seen on 08/10: 92 opened tabs, 2 of them Zen folder placeholders.
        code,payload=self.report(['a','b','folder-empty'],[record('a'),record('b')],['folder-empty'])
        self.assertEqual(code,0);self.assertEqual(payload['structuralIds'],['folder-empty'])
        self.assertEqual([r['id'] for r in payload['records']],['a','b'])
    def test_real_tab_that_cannot_be_exported_still_fails(self):
        code,payload=self.report(['a','lost'],[record('a')],[])
        self.assertEqual(code,1);self.assertFalse(payload['ok']);self.assertEqual(payload['exported'],1)
    def test_allow_excluded_lists_both_kinds(self):
        code,payload=self.report(['a','lost','empty'],[record('a')],['empty'],allow_excluded=True)
        self.assertEqual(code,0);self.assertEqual(payload['excludedIds'],['empty','lost'])

if __name__=='__main__': unittest.main()
