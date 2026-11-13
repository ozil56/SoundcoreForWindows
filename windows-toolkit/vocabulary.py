"""Local vocabulary, explicit review and immutable transcription snapshots."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import threading
import uuid
from tool_settings import read_settings, write_json


def now():
    return dt.datetime.now().astimezone().isoformat(timespec='seconds')


def parse_terms(text):
    terms = list(dict.fromkeys(line.strip() for line in text.lstrip('\ufeff').splitlines() if line.strip()))
    if any(len(term) > 160 or any(ord(c) < 32 for c in term) for term in terms):
        raise ValueError('每行一个词或短语，最长 160 字；请去掉制表符、编号和说明。')
    return terms


class VocabularyStore:
    def __init__(self, root):
        self.folder = Path(read_settings(root).get('vocabulary_dir') or Path(root)/'词库')
        self.path = self.folder/'通用词库.json'
        self.lock = threading.RLock()
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding='utf-8'))
        else:
            self.data = {'revision':0, 'terms':[], 'audit':[]}

    def rows(self, state=None):
        with self.lock:
            return copy.deepcopy([r for r in self.data['terms'] if state is None or r['state']==state])

    def commit(self, action, before):
        self.data['revision'] += 1
        self.data['audit'].append({'at':now(), 'action':action, 'before':before,
                                   'revision':self.data['revision']})
        write_json(self.path, self.data)
        self.export(self.folder/'通用词库.txt')

    def add(self, terms, source, state='accepted', context=''):
        if state not in ('accepted','pending'): raise ValueError('无效词条状态')
        terms = parse_terms('\n'.join(terms))
        with self.lock:
            before = copy.deepcopy(self.data['terms'])
            known = {r['term']:r for r in self.data['terms']}
            added = 0
            for term in terms:
                if term in known: continue
                row = {'id':uuid.uuid4().hex, 'term':term, 'source':source,
                       'context':context, 'state':state, 'created_at':now()}
                self.data['terms'].append(row); known[term]=row; added+=1
            if added: self.commit(f'导入 {added} 个词条（{state}）', before)
            return added

    def review(self, ids, state, edited_term=None):
        if state not in ('accepted','ignored','disabled','pending'): raise ValueError('无效审核状态')
        with self.lock:
            before=copy.deepcopy(self.data['terms'])
            if edited_term is not None:
                terms=parse_terms(edited_term)
                if len(terms)!=1 or len(ids)!=1: raise ValueError('一次编辑一个词条')
                if any(r['term']==terms[0] and r['id'] not in ids for r in before):
                    raise ValueError('词条已存在')
            for row in self.data['terms']:
                if row['id'] in ids:
                    row.update(state=state, reviewed_at=now())
                    if edited_term is not None: row['term']=terms[0]
            self.commit(f'审核/编辑 {len(ids)} 个词条 → {state}', before)

    def undo(self):
        with self.lock:
            audit = self.data['audit']
            if not audit: return False
            before=copy.deepcopy(self.data['terms'])
            self.data['terms']=copy.deepcopy(audit[-1]['before'])
            self.commit('恢复上一次操作前的词库', before)
            return True

    def export(self, path):
        path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('\n'.join(r['term'] for r in self.rows('accepted'))+'\n',encoding='utf-8')

    def snapshot(self, special=()):
        with self.lock:
            data={'general_revision':self.data['revision'],
                  'general':[r['term'] for r in self.rows('accepted')],
                  'special':parse_terms('\n'.join(special)), 'created_at':now()}
            data['sha256']=hashlib.sha256(json.dumps({k:data[k] for k in ('general_revision','general','special')},
                sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            target=self.folder/'快照'/(data['sha256']+'.json')
            if not target.exists(): write_json(target,data)
            return data
