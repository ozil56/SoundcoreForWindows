"""Recording metadata and append-only transcript versions, shared with the durable queue."""
import copy
import datetime as dt
import hashlib
from pathlib import Path
import shutil
import uuid
from vocabulary import now
from tool_settings import write_json


class CatalogMixin:
    def migrate_catalog(self):
        self.data.setdefault('records',{})
        self.data.setdefault('projects',[])
        for key,job in self.data['jobs'].items():
            if job.get('manual'): continue
            record=self._ensure_record(key,job['receipt'])
            if job.get('output') and not record['versions']:
                version={'id':'original','output':job['output'],'label':'原转写结果',
                         'created_at':job.get('completed_at',''), 'vocabulary':{},'job_key':key}
                record['versions'].append(version); record['current_version']='original'

    def _ensure_record(self,key,receipt):
        return self.data['records'].setdefault(key,{'id':key,'receipt':copy.deepcopy(receipt),
            'title':Path(receipt['path']).stem,'project':'','versions':[], 'current_version':None,'special':[]})

    def records(self):
        with self.lock:
            return copy.deepcopy(list(self.data['records'].values()))

    def edit_record(self,keys,project=None,title=None):
        with self.lock:
            for key in keys:
                record=self.data['records'][key]
                if project is not None: record['project']=project.strip()
                if title is not None: record['title']=title.strip() or record['title']
            if project and project.strip() not in self.data['projects']: self.data['projects'].append(project.strip())
            self.save()

    def queue_manual(self,record_id,snapshot):
        with self.lock:
            record=self.data['records'][record_id]
            token=uuid.uuid4().hex
            key=record_id+':manual:'+token
            versions=record['versions']
            reference=next((v for v in reversed(versions) if Path(v['output']+'.json').exists()),None)
            self.data['jobs'][key]={'receipt':copy.deepcopy(record['receipt']), 'record_id':record_id,
                'manual':True, 'version_id':token, 'created_at':now(), 'status':'pending','attempts':0,'retry_at':0,
                'vocabulary':copy.deepcopy(snapshot),'project':record['project'],'title':record['title'],
                'reference':reference['output']+'.json' if reference else None}
            record['special']=snapshot['special'].copy()
            self.save()
            return key

    def finish_version(self,key,output,job,output_root=None):
        with self.lock:
            record=self.data['records'][job.get('record_id',key)]
            token=job.get('version_id') or uuid.uuid4().hex
            if not any(v['id']==token for v in record['versions']):
                record['versions'].append({'id':token,'job_key':key,'output':str(output),
                    'label':'专用词库重新转写' if job.get('manual') else '自动转写',
                    'created_at':now(),'project':job.get('project',record['project']),
                    'vocabulary':copy.deepcopy(job.get('vocabulary',{}))})
            record['current_version']=token
            self.data['jobs'][key].update(status='complete',output=str(output),
                output_root=str(Path(output_root or Path(output).parent).resolve()),error='',retry_at=0,completed_at=now())
            self.save()

    def select_version(self,record_id,version_id):
        with self.lock:
            record=self.data['records'][record_id]
            if not any(v['id']==version_id for v in record['versions']): raise ValueError('版本不存在')
            record['current_version']=version_id
            self.save()

    def cancel(self,key):
        with self.lock:
            job=self.data['jobs'][key]
            if job['status'] not in ('pending','running','failed'): return
            job['status']='cancelled'; self.save()

    def has_manual(self):
        with self.lock:
            return any(j.get('manual') and j['status'] in ('pending','failed') and j.get('retry_at',0)<=__import__('time').time()
                       for j in self.data['jobs'].values())

    def import_audio(self,source,folder):
        source=Path(source)
        with source.open('rb') as file: digest=hashlib.file_digest(file,'sha256').hexdigest()
        with self.lock:
            for record in self.data['records'].values():
                if record['receipt']['sha256']==digest: return record['id']
        folder=Path(folder); folder.mkdir(parents=True,exist_ok=True)
        target=folder/(source.stem+'_'+digest[:10]+source.suffix.lower())
        if not target.exists():
            temporary=target.with_suffix(target.suffix+'.part')
            shutil.copy2(source,temporary)
            with temporary.open('rb') as file:
                if hashlib.file_digest(file,'sha256').hexdigest()!=digest: raise ValueError('导入校验失败')
            temporary.replace(target)
        else:
            with target.open('rb') as file:
                if hashlib.file_digest(file,'sha256').hexdigest()!=digest: raise ValueError('目标音频已存在但内容不同')
        date=dt.datetime.fromtimestamp(source.stat().st_mtime).astimezone()
        receipt={'identity_hex':'external','file_id':int(date.timestamp()), 'path':str(target.resolve()),
            'sha256':digest,'bytes':target.stat().st_size,'complete':True,'seconds':0,'external_source':str(source),
            'recorded_at':date.isoformat(),'date_source':'external_file_modified_time'}
        write_json(target.with_suffix(target.suffix+'.json'),receipt)
        return self.register(receipt)
