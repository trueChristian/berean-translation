from __future__ import annotations
import copy
import json
import shutil
from pathlib import Path
from urllib.parse import urlsplit
from berean_translation.common import canonical, digest, json_hash, loads, read_json
from berean_translation.config import Config
from berean_translation.engine import Engine
from berean_translation.source import SourceClient
from berean_translation.state import State

REPO_ROOT = Path(__file__).resolve().parents[1]
A = '11111111-1111-4111-8111-111111111111'
B = '22222222-2222-4222-8222-222222222222'
ISSUE = '33333333-3333-4333-8333-333333333333'
ISSUE2 = '44444444-4444-4444-8444-444444444444'


class FixtureSource:
    def __init__(self, root, articles=2):
        self.revision = 'a'*40
        self.contents = {}
        self.articles = []
        for n, identity in enumerate((A,B)[:articles],1):
            text = (f'<article data-article-id="{identity}"><p>Faith and <em>grace</em>. John 3:16–18.</p>'
                    f'<figure><img src="/images/articles/{identity}-1.jpg" alt="A tree">'
                    '<figcaption>A tree.</figcaption></figure></article>')
            item = {'id':identity,'issue_id':ISSUE,'sequence':n,'title':'Faith','subtitle':None,'section':'Teaching',
                    'language':'en','html':{'repository_path':f'content/articles/{identity}.html'},
                    'images':[{'public_path':f'/images/articles/{identity}-1.jpg','alt':'A tree','caption':'A tree.'}],
                    'rights':{'status':'eligible','article_specific_permission_notice_detected':False}}
            self.articles.append(item)
            self.contents[item['html']['repository_path']] = text
        self.issues = [{'id':ISSUE,'slug':'heartbeat-remnant-2024-summer','source_id':'heartbeat-remnant-2024-summer',
                        'date':{'year':2024,'season':'summer'},'publication':'Test fixture, not a published article'}]
        self.rebuild()
        self.client = SourceClient(Config(root),fetch=self.fetch)

    def rebuild(self):
        index = {'format_version':'2.0','articles':self.articles,'skipped':[]}
        catalogue = {'format_version':'2.0','issues':self.issues,'categories':[],'topics':[],'series':[]}
        fingerprints = {}
        for item in self.articles:
            text = self.contents[item['html']['repository_path']]
            fingerprints[item['id']] = {'html_sha256':digest(text),'text_sha256':digest(text),
                'structure_sha256':digest('fixture structure'),
                'translation_metadata_sha256':json_hash({k:item.get(k) for k in ('title','subtitle','section','images')})}
        index_raw = json.dumps(index,ensure_ascii=False,indent=2)+'\n'
        catalogue_raw = json.dumps(catalogue,ensure_ascii=False,indent=2)+'\n'
        self.manifest = {'format_version':'2.0','index_sha256':digest(index_raw),'catalogue_sha256':digest(catalogue_raw),
                         'articles':fingerprints,'content_sha256':digest(canonical(index))}
        self.contents.update({'index.json':index_raw,'catalogue.json':catalogue_raw,
                              'manifest.json':canonical(self.manifest).decode()})

    def fetch(self,url):
        if 'api.github.com' in url:
            return canonical({'sha':self.revision})
        marker = '/' + self.revision + '/'
        if marker not in url:
            raise AssertionError('Snapshot was not pinned to the expected commit')
        return self.contents[url.split(marker,1)[1]].encode('utf-8')

    def change_source(self, text='Grace changed. John 3:16–18.'):
        self.revision = 'b'*40
        self.contents[f'content/articles/{A}.html'] = self.contents[f'content/articles/{A}.html'].replace(
            'Faith and <em>grace</em>. John 3:16–18.', 'Faith and <em>grace</em>. '+text)
        self.rebuild()


class MemoryGit:
    def __init__(self):
        self.checkpoints = []
        self.bot = False
    def checkpoint(self,message):
        self.checkpoints.append(message)
    def human_edit_evidence(self,path):
        if self.bot:
            from berean_translation.common import ContractError
            raise ContractError('A bot cannot claim human review')
        return {'commit':'c'*40,'author':'Fixture reviewer','email':'reviewer@example.test','time':'2026-01-01T00:00:00Z'}


class FakeProvider:
    """API simulator: no network, credentials, charges, or genuine translations."""
    def __init__(self):
        self.files, self.batches = {},{}
        self.create_calls = 0
        self.upload_calls = 0
        self.raise_create = None
        self.raise_upload = False

    def upload(self,payload,name):
        self.upload_calls += 1
        if self.raise_upload:
            raise TimeoutError('simulated upload error')
        identity = 'file-'+str(len(self.files)+1)
        self.files[identity] = payload
        return identity

    def create(self,input_file_id,key,campaign):
        self.create_calls += 1
        if self.raise_create == 'before':
            raise TimeoutError('simulated unknown create result')
        identity = 'batch-'+str(len(self.batches)+1)
        item = {'id':identity,'input_file_id':input_file_id,'status':'in_progress','metadata':{'submission_key':key,'campaign':campaign}}
        self.batches[identity] = item
        if self.raise_create == 'after':
            raise TimeoutError('simulated lost success response')
        return copy.deepcopy(item)

    def retrieve(self,identity):
        return copy.deepcopy(self.batches[identity])

    def find(self,key):
        return [copy.deepcopy(b) for b in self.batches.values() if b['metadata']['submission_key']==key]

    def content(self,identity):
        return self.files[identity]

    def cancel(self,identity):
        self.batches[identity]['status'] = 'cancelled'
        return self.retrieve(identity)

    @staticmethod
    def default_result(line):
        stage = line['custom_id'].split(':')[1]
        if stage.startswith('review'):
            return {'score':97,'passed':True,'findings':[]}
        source = loads(line['body']['messages'][1]['content'])['source']
        return {key:source[key] for key in ('html','title','subtitle','section')}

    def complete_all(self,responder=None,status='completed',reverse=True):
        for batch in self.batches.values():
            if batch['status'] != 'in_progress':
                continue
            lines = [loads(l) for l in self.files[batch['input_file_id']].splitlines()]
            results = []
            for line in lines:
                result = (responder or self.default_result)(line)
                if result is None:
                    continue
                row = {'custom_id':line['custom_id'],'error':None,'response':{'status_code':200,
                       'request_id':'req-test','body':{'model':line['body']['model'],'id':'chat-test',
                       'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result),'refusal':None}}],
                       'usage':{'prompt_tokens':200,'completion_tokens':100}}}}
                results.append(row)
            if reverse:
                results.reverse()
            output_id = batch['id']+'-output'
            self.files[output_id] = b'\n'.join(canonical(row) for row in results)
            batch.update(status=status,output_file_id=output_id)


def setup(root):
    shutil.copytree(REPO_ROOT/'config',root/'config')
    shutil.copytree(REPO_ROOT/'prompts',root/'prompts')
    (root/'content').mkdir()
    state = State(root)
    config = Config(root)
    state.derive(config)
    upstream = FixtureSource(root)
    provider = FakeProvider()
    git = MemoryGit()
    engine = Engine(config,upstream.client,provider,git)
    return config,state,upstream,provider,git,engine


def queue(state,identity='request-1',**changes):
    request = {'id':identity,'operation':'translate','languages':'afr','issues':'next',
               'model':'gpt-4.1-mini','review_model':'gpt-4.1-mini','budget_usd':5,
               'dry_run':False,'retry_failed':False,'requested_by':'fixture-owner'}
    request.update(changes)
    state.write(f'state/queue/{identity}.json',request)
    return request


def drive(engine,provider,responder=None,ticks=6):
    for _ in range(ticks):
        engine.tick()
        provider.complete_all(responder)
