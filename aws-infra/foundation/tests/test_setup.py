"""Tool download integrity and extraction security survive Python -O."""
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile

SETUP=Path(__file__).resolve().parents[1]/'scripts/setup-local.py'

class SetupTests(unittest.TestCase):
    def check_optimized(self,action,contents,expected=None):
        with tempfile.TemporaryDirectory(prefix='foundation-setup-') as temp:
            folder=Path(temp);archive=folder/'archive.zip';archive.write_bytes(contents)
            code='import runpy,sys\nm=runpy.run_path(sys.argv[1])\n'
            if action=='hash':code+='m["verify_checksum"](sys.argv[2],sys.argv[4])\n'
            elif action=='lock':code+='m["verify_provider_lock"](sys.argv[2],sys.argv[4])\n'
            else:code+='m["extract_flat_archive"](sys.argv[2],sys.argv[3])\n'
            env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')
            process=subprocess.run([sys.executable,'-O','-c',code,str(SETUP),str(archive),str(folder/'destination'),expected or ''],env=env,capture_output=True,text=True)
            return process.returncode,sorted(p.name for p in (folder/'destination').glob('*')),process.stderr
    def archive(self,entries):
        buf=io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore',UserWarning)
            with zipfile.ZipFile(buf,'w') as payload:
                for name,value,mode in entries:
                    entry=zipfile.ZipInfo(name);entry.create_system=3;entry.external_attr=mode<<16
                    payload.writestr(entry,value)
        return buf.getvalue()
    def test_optimized_checksum_and_signed_lock_reject_tampered_archive(self):
        good=self.archive([('terraform',b'accepted-binary',0o100755)])
        digest=hashlib.sha256(good).hexdigest()
        for kind,expected in [('hash',digest),('lock','hashes = ["zh:'+digest+'"]')]:
            with self.subTest(kind=kind):
                self.assertEqual(self.check_optimized(kind,good,expected)[0],0)
                self.assertNotEqual(self.check_optimized(kind,good+b'tampering',expected)[0],0)
                self.assertNotEqual(self.check_optimized(kind,good,'0'*64)[0],0)
    def test_optimized_flat_archive_accepts_regular_binary(self):
        code,files,error=self.check_optimized('extract',self.archive([('terraform',b'accepted',0o100755)]))
        self.assertEqual(code,0,error);self.assertEqual(files,['terraform'])
    def test_optimized_unsafe_link_device_duplicate_rejected_before_extract(self):
        cases=[ [('terraform',b'ok',0o100755),('../escape',b'bad',0o100644)],
                [('/absolute',b'bad',0o100644)], [('nested/path',b'bad',0o100644)],
                [('back\\slash',b'bad',0o100644)], [('link',b'/etc/passwd',0o120777)],
                [('device',b'',0o020600)], [('same',b'a',0o100644),('same',b'b',0o100644)]]
        for entries in cases:
            with self.subTest(entries=[e[0] for e in entries]):
                code,files,error=self.check_optimized('extract',self.archive(entries))
                self.assertNotEqual(code,0);self.assertEqual(files,[])
