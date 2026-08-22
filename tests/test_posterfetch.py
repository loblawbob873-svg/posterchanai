import json
import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class PosterfetchTests(unittest.TestCase):
    def test_render_is_fast_self_contained_and_has_the_promised_stats(self):
        js = r"""
const p=require('./desktop/posterfetch.js');
const out=p.render({USER:'cyber',HOME:process.env.HOME,SHELL:'/bin/bash',XDG_SESSION_TYPE:'wayland'});
console.log(JSON.stringify({out, ms:Date.now()-start}));
"""
        # Define the clock before requiring the module so module startup is included.
        js = "const start=Date.now();\n" + js
        got = json.loads(subprocess.check_output(['node', '-e', js], cwd=ROOT, text=True))
        out = got['out']
        for label in ('OS', 'Kernel', 'Uptime', 'CPU', 'RAM', 'GPU', 'Disk', 'Network', 'Session'):
            self.assertIn(label, out)
        self.assertIn('POSTERCHAN // OWN YOUR SIGNAL', out)
        self.assertIn('cyber@', out)
        self.assertLess(got['ms'], 1000)

    def test_helpers_do_not_lie_at_boundaries(self):
        js = "const p=require('./desktop/posterfetch.js'); console.log(JSON.stringify([p.human(1073741824),p.duration(90061)]))"
        got = json.loads(subprocess.check_output(['node', '-e', js], cwd=ROOT, text=True))
        self.assertEqual(got, ['1.0 GiB', '1d 1h 1m'])

    def test_each_new_local_tab_buffers_one_welcome(self):
        src = (ROOT / 'desktop/localterm.js').read_text()
        self.assertIn("require('./posterfetch.js')", src)
        self.assertIn('buf: welcome', src)
        self.assertIn('seq: welcome.length', src)


if __name__ == '__main__':
    unittest.main()
