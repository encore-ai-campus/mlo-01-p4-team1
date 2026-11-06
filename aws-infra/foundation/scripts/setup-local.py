#!/usr/bin/env python3
"""Install exactly locked tools under foundation/.cache; no AWS CLI/API usage."""
import hashlib,json,os,platform,re,shutil,stat,subprocess,sys,zipfile
from pathlib import Path
BASE=Path(__file__).resolve().parents[1]
LOCK=json.loads((BASE/'toolchain.lock.json').read_text())

def download(url,dest):
    subprocess.run(['curl','--fail','--location','--retry','2','--connect-timeout','15','--max-time','180',url,'-o',str(dest)],check=True)

def require(condition,message):
    if not condition:raise ValueError(message)

def verify_checksum(archive,expected):
    require(isinstance(expected,str) and re.fullmatch(r'[a-f0-9]{64}',expected),'invalid trusted checksum')
    digest=hashlib.sha256()
    with Path(archive).open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):digest.update(chunk)
    require(digest.hexdigest()==expected,'toolchain archive checksum mismatch')

def verify_provider_lock(archive,trusted):
    hashes=set(re.findall(r'"zh:([a-f0-9]{64})"',trusted))
    digest=hashlib.sha256(Path(archive).read_bytes()).hexdigest()
    require(digest in hashes,'AWS provider archive does not match signed lock')

def extract_flat_archive(archive,destination,*,only=None):
    """Validate every member before touching files; optimization cannot skip checks."""
    destination=Path(destination)
    with zipfile.ZipFile(archive) as payload:
        entries=payload.infolist();names=set();total=0
        require(0<len(entries)<=32,'invalid toolchain archive member count')
        for entry in entries:
            require(re.fullmatch(r'[A-Za-z0-9._-]+',entry.filename) and entry.filename not in {'.','..'},'unsafe toolchain archive path')
            require(entry.filename not in names and not entry.is_dir(),'duplicate/directory toolchain member')
            mode=entry.external_attr>>16
            require(stat.S_IFMT(mode) in {0,stat.S_IFREG},'toolchain archive requires regular files')
            require(not entry.flag_bits&1 and 0<=entry.file_size<=1024*1024*1024,'encrypted/oversized toolchain member')
            names.add(entry.filename);total+=entry.file_size
        require(total<=1536*1024*1024,'expanded toolchain archive too large')
        require(only is None or only in names,'expected toolchain binary missing')
        destination.mkdir(parents=True,exist_ok=True)
        for entry in entries:
            if only is not None and entry.filename!=only:continue
            target=destination/entry.filename
            require(not target.is_symlink(),'toolchain destination symlink refused')
            # A temporary file avoids partial replacement of an existing executable.
            temporary=destination/('.'+entry.filename+'.download-'+str(os.getpid()))
            try:
                with payload.open(entry) as source,temporary.open('xb') as output:
                    shutil.copyfileobj(source,output)
                    output.flush();os.fsync(output.fileno())
                os.replace(temporary,target)
            finally:temporary.unlink(missing_ok=True)

def main():
    target={('Darwin','arm64'):'darwin_arm64',('Linux','x86_64'):'linux_amd64'}.get((platform.system(),platform.machine()))
    if target is None:raise SystemExit('BLOCKED: supported tool platforms: darwin_arm64, linux_amd64')
    cache=BASE/'.cache/toolchain';cache.mkdir(parents=True,exist_ok=True)
    filename=f'terraform_1.11.4_{target}.zip';archive=cache/filename
    download(LOCK['terraform']['source']+filename,archive)
    verify_checksum(archive,LOCK['terraform']['sha256'][filename])
    extract_flat_archive(archive,cache,only='terraform')
    (cache/'terraform').chmod(0o755)
    provider_name=f'terraform-provider-aws_5.94.1_{target}.zip';provider=cache/provider_name
    download('https://releases.hashicorp.com/terraform-provider-aws/5.94.1/'+provider_name,provider)
    trusted=(BASE/'terraform/environments/dev/encore1/.terraform.lock.hcl').read_text()
    verify_provider_lock(provider,trusted)
    mirror=BASE/'.cache/provider-mirror/registry.terraform.io/hashicorp/aws/5.94.1'/target;mirror.mkdir(parents=True,exist_ok=True)
    extract_flat_archive(provider,mirror)
    for binary in mirror.glob('terraform-provider-aws*'):binary.chmod(0o755)
    print('PASS: pinned Terraform and AWS provider installed in foundation-only cache')
if __name__=='__main__':main()
