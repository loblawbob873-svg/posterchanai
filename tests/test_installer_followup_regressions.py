"""Installer fallback/configuration behavior, without installing packages or touching host paths."""
import importlib.util
import io
import os
from pathlib import Path
import shlex
import subprocess
import pytest

ROOT=Path(os.environ.get('PC_INSTALLER_REVIEW_ROOT',Path(__file__).resolve().parents[1]))
SOURCE=(ROOT/'os/gentoo.sh').read_text()


def shell(script, **values):
    result=subprocess.run(['bash','-eu','-c',script],env={**os.environ,**{k:str(v) for k,v in values.items()}},capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    return result.stdout


@pytest.mark.parametrize('target',['','/','temporary'])
def test_shell_fallback_defines_target_runner_without_overlay_config(tmp_path,target):
    start=SOURCE.index('\t# Success is checked by looking for the FILES',SOURCE.index('posterchanShell() {'))
    end=SOURCE.index('\t\techo -e "\\033[1;33mSyncing the PosterChanOS overlay',start)
    code=SOURCE[start:end].replace('"${TARGET}/etc/portage/repos.conf/posterchan.conf"',shlex.quote(str(tmp_path/'missing.conf')))
    target=str(tmp_path/'target') if target=='temporary' else target
    script='''
chroot(){ printf '%s\\n' "$1" > "$CHROOT_SEEN";shift;"$@"; }
'''+code+'''
 :
fi
_in 'printf ready > "$PROBE"'
'''
    probe=tmp_path/'probe';seen=tmp_path/'chroot'
    shell(script,TARGET=target,PROBE=probe,CHROOT_SEEN=seen)
    assert probe.read_text()=='ready'
    if target not in ('','/'):assert seen.read_text().strip()==target
    else:assert not seen.exists()


@pytest.mark.parametrize('kind',['file','directory','absent'])
def test_keyword_updates_preserve_operator_entries_and_are_idempotent(tmp_path,kind):
    base=tmp_path/'portage';base.mkdir();keywords=base/'package.accept_keywords'
    original='# my workstation\napp-editors/custom ~amd64\n'
    if kind=='file':keywords.write_text(original)
    elif kind=='directory':keywords.mkdir();(keywords/'operator').write_text(original)
    start=SOURCE.index('unmaskPackages() {');end=SOURCE.index('\nupdateOS() {',start)
    function=SOURCE[start:end].replace('/etc/portage',str(base))
    shell(function+'''\nSPECIAL_PACKAGE_USE=();MASKED_PACKAGES=(app-misc/example sys-apps/another)
unmaskPackages
unmaskPackages
''')
    assert keywords.is_dir(),'keyword configuration was not migrated to managed files'
    if kind!='absent':assert (keywords/('00-local' if kind=='file' else 'operator')).read_text()==original
    assert (keywords/'posterchan-managed').read_text()=='app-misc/example ~amd64\nsys-apps/another ~amd64\n'
    assert len(list(keywords.glob('.posterchan-managed.*')))==0


@pytest.mark.parametrize('cached_tar,cached_image,remote_tar,expected',[
    (b'<html>old</html>',None,b'\x28\xb5\x2f\xfdnew','tar'),
    (b'<html>old</html>',b'\x7fELFcached',b'<html>bad</html>','image'),
    (None,b'<html>old</html>',b'<html>bad</html>','image'),
    (b'\x28\xb5\x2f\xfdcached',None,b'<html>bad</html>','cached-tar'),
])
def test_cached_archives_are_validated_before_download_fallback(tmp_path,cached_tar,cached_image,remote_tar,expected):
    tar=tmp_path/'desktop.tar.zst';image=tmp_path/'desktop.AppImage'
    if cached_tar:tar.write_bytes(cached_tar)
    if cached_image:image.write_bytes(cached_image)
    tar_source=tmp_path/'remote.tar';tar_source.write_bytes(remote_tar)
    image_source=tmp_path/'remote.image';image_source.write_bytes(b'\x7fELFfresh')
    start=SOURCE.index('\t_pc_magic4()');end=SOURCE.index('\tif [ -s "$APPTAR" ]; then',start)
    code=SOURCE[start:end]
    mock='''
curl(){
 local output=''
 while [ "$#" -gt 0 ];do
  if [ "$1" = '-o' ];then output="$2";shift;fi
  shift
 done
 printf 'fetch\\n' >> "$FETCH_SEEN"
 if [ -z "$output" ];then printf '{}';return;fi
 if [ "$output" = "$APPTAR" ];then cp "$TAR_SOURCE" "$output";else cp "$IMAGE_SOURCE" "$output";fi
}
'''
    seen=tmp_path/'fetches'
    shell(mock+code,APPTAR=tar,APPIMG=image,TAR_SOURCE=tar_source,IMAGE_SOURCE=image_source,
          FETCHLOG=tmp_path/'fetch.log',FETCH_SEEN=seen,PP='https://fixture.invalid',GH='https://fixture.invalid')
    if expected.endswith('tar'):
        assert tar.read_bytes().startswith(b'\x28\xb5\x2f\xfd')
        if expected=='cached-tar':assert not seen.exists(),'valid cached archive was redownloaded'
    else:
        assert not tar.exists(),'invalid tarball still prevents valid AppImage fallback'
        assert image.read_bytes().startswith(b'\x7fELF')


@pytest.mark.parametrize('url_path,status',[
    ('/normal.txt',200),('/../root-other/secret.txt',404),('/escape',404),('/missing',404),
])
def test_scratch_file_server_rejects_sibling_prefix_and_symlink_escape(tmp_path,url_path,status):
    root=tmp_path/'root';root.mkdir();(root/'normal.txt').write_bytes(b'normal')
    sibling=tmp_path/'root-other';sibling.mkdir();(sibling/'secret.txt').write_bytes(b'private')
    (root/'escape').symlink_to(sibling/'secret.txt')
    spec=importlib.util.spec_from_file_location('review_scratch_gate',ROOT/'scripts/check_scratch_install_vm.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    handler=object.__new__(module.Serve);handler.root=root;handler.path=url_path;handler.wfile=io.BytesIO()
    codes=[];handler.send_response=codes.append;handler.send_error=codes.append
    handler.send_header=lambda *_:None;handler.end_headers=lambda:None
    handler.do_GET()
    assert codes==[status]
    assert handler.wfile.getvalue()==(b'normal' if status==200 else b'')
