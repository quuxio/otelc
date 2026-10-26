import hashlib,json,subprocess,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve();dest=Path(sys.argv[2]).resolve();base=sys.argv[3];dest.mkdir(parents=True,exist_ok=True)
head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
assert not subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=root)
archive=dest/'source.tar'
with archive.open('xb') as out: subprocess.run(['git','archive','--format=tar',head],cwd=root,stdout=out,check=True)
facts={};extra={}
for raw in subprocess.check_output(['git','ls-files','-z'],cwd=root).split(b'\0'):
 if not raw:continue
 name=raw.decode();p=root/name;s=p.stat();facts[name]=[hashlib.sha256(p.read_bytes()).hexdigest(),s.st_dev,s.st_ino,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns];extra[name]=[s.st_uid,s.st_gid,s.st_nlink]
value=dict(root=str(root),head=head,base=base,source_facts=facts,extra_facts=extra,archive=str(archive),archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
(dest/'source.json').write_text(json.dumps(value,indent=2)+'\n')
print(head,len(facts),value['archive_sha256'])
