import subprocess, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]

def test_real_picker_runs_and_fits_android_phone():
    p=subprocess.run([sys.executable,str(ROOT/'scripts/check_blossom_picker_mobile.py')],cwd=ROOT,text=True,capture_output=True)
    assert p.returncode in (0,2),p.stdout+p.stderr
