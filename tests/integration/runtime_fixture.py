"""CI-only HTTP/Airflow fixtures. Never included in a production image closure.

Actual role images, PostgreSQL, MinIO, source loaders, artifact sync, guard and
Kubernetes controllers are used. HTTP business handlers are fixtures; inference
loads and predicts with the real offline RandomForest artifact. This does not
claim to test every production API route or a real Kaggle training request.
"""
import argparse
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import sys
import threading
import time
from urllib.parse import urlsplit,parse_qs

sys.path.insert(0,'/app')
MODE='role';LOCK=threading.Lock();INFLIGHT=0;DRAINING=False;PAUSED={};PROVIDER_CALLS=0;DENY_UPLOAD=False;RUNNING=0


def identity():
    path=Path('/app/role-release.json')
    if path.exists():
        from deployment.rke2.runtime.role_runtime import verify
        doc=verify()
        return {'status':'ok','commit':doc['commit'],'release_id':doc['source_id'],'source_id':doc['source_id'],
                'image_role':doc['role'],'role':'api' if doc['role']=='api' else 'tree' if doc['role']=='tree' else 'recurrent',
                'runtime_contract':'traffic-runtime-contract/v1'}
    doc=json.loads(Path('/opt/traffic-release/release.json').read_text())
    return {'status':'ok','commit':doc['commit'],'release_id':doc['release_id']}


def inference():
    import joblib
    import numpy as np
    import pandas as pd
    model=joblib.load('/app/models/ci-probe.joblib')
    values=np.load('/app/data/ci-input.npy',allow_pickle=False)
    predicted=model.predict(pd.DataFrame(values,columns=['a','b','c']))
    expected=json.loads(Path('/fixtures/expected.json').read_text())['models']['random_forest']['expected']
    np.testing.assert_allclose(predicted,expected,rtol=1e-5,atol=1e-5)
    return {'fixture_inference_verified':True,'rows':len(predicted)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def send_json(self,value,status=200):
        raw=json.dumps(value).encode();self.send_response(status)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)))
        self.end_headers();self.wfile.write(raw)
    def do_GET(self):self.handle_request()
    def do_POST(self):self.handle_request()
    def do_PATCH(self):self.handle_request()
    def do_PUT(self):self.handle_request()
    def do_HEAD(self):self.handle_request()
    def do_DELETE(self):self.handle_request()
    def body(self):
        if self.headers.get('Transfer-Encoding','').lower()!='chunked':
            return self.rfile.read(int(self.headers.get('Content-Length','0')))
        chunks=[]
        while True:
            length=int(self.rfile.readline().split(b';',1)[0],16)
            if not length:
                while self.rfile.readline().strip():pass
                return b''.join(chunks)
            chunks.append(self.rfile.read(length));self.rfile.read(2)
    def handle_request(self):
        global INFLIGHT,DRAINING,PROVIDER_CALLS,DENY_UPLOAD,RUNNING
        tracked=MODE=='role' and '/health/' not in self.path and '/internal/lifecycle/' not in self.path
        if tracked:
            with LOCK:INFLIGHT+=1
        try:
            path=urlsplit(self.path).path
            if MODE=='s3':
                body=self.body()
                if path=='/_fixture/deny':
                    DENY_UPLOAD=json.loads(body)['deny'];return self.send_json({'denying_synthetic_object':DENY_UPLOAD})
                if DENY_UPLOAD and self.command=='PUT' and path=='/ci-runtime/models/unacknowledged.txt':
                    raw=b'<Error><Code>AccessDenied</Code><Message>isolated fixture</Message></Error>'
                    self.send_response(403);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
                upstream=http.client.HTTPConnection('minio-storage',9000,timeout=30)
                try:
                    headers={k:v for k,v in self.headers.items() if k.lower() not in {'transfer-encoding','content-length','connection','expect'}}
                    headers['Content-Length']=str(len(body));upstream.request(self.command,self.path,body=body,headers=headers)
                    response=upstream.getresponse();raw=response.read();self.send_response(response.status)
                    for k,v in response.getheaders():
                        if k.lower() not in {'transfer-encoding','connection','content-length'}:self.send_header(k,v)
                    self.send_header('Content-Length',response.getheader('Content-Length','0') if self.command=='HEAD' else str(len(raw)))
                    self.end_headers()
                    if self.command!='HEAD':self.wfile.write(raw)
                finally:upstream.close()
            elif MODE=='proxy':
                upstream=http.client.HTTPConnection('traffic-api',8000,timeout=90)
                try:
                    upstream.request(self.command,self.path)
                    response=upstream.getresponse();raw=response.read()
                    self.send_response(response.status);self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
                finally:upstream.close()
            elif MODE=='airflow':
                if path=='/_fixture/busy':
                    RUNNING=1 if json.loads(self.body())['busy'] else 0;self.send_json({'running':RUNNING})
                elif '/dagRuns' in path:self.send_json({'total_entries':RUNNING if 'state=running' in self.path else 1,'dag_runs':[]})
                elif '/dags/' in path:
                    dag=path.split('/dags/',1)[1]
                    if self.command=='PATCH':PAUSED[dag]=json.loads(self.rfile.read(int(self.headers['Content-Length'])))['is_paused']
                    self.send_json({'is_paused':PAUSED.get(dag,False)})
                else:self.send_json({'paused':PAUSED,'queued':1,'provider_calls':PROVIDER_CALLS})
            elif '/internal/lifecycle/' in path:
                if self.headers.get('X-Internal-Token')!=os.environ['INTERNAL_TRAINING_TOKEN']:
                    return self.send_json({'error':'unauthorized'},403)
                if path.endswith('/drain'):DRAINING=True
                if path.endswith('/resume'):DRAINING=False
                self.send_json({'draining':DRAINING,'inflight':INFLIGHT,'warmed':True})
            elif path.endswith('/health/release'):self.send_json(identity())
            elif path.endswith('/health/serving-ready') and DRAINING:self.send_json({'status':'draining'},503)
            elif '/health/' in path or MODE=='health':self.send_json({'status':'ok' if path.endswith('/health/live') else 'ready'})
            elif '/internal/training-runs/' in path:
                PROVIDER_CALLS+=1;self.send_json({'error':'Provider is disabled in this fixture'},409)
            elif path.endswith('/_fixture/slow'):
                time.sleep(float(parse_qs(urlsplit(self.path).query).get('seconds',['45'])[0]));self.send_json({'request_completed':True})
            elif path.endswith('/prediction-window'):self.send_json(inference())
            elif '/model-quality' in path and os.environ.get('FIXTURE_FAIL_SMOKE')=='true':self.send_json({'error':'fixture_failure'},503)
            else:self.send_json({'fixture':True})
        except Exception as error:self.send_json({'error_type':type(error).__name__},500)
        finally:
            if tracked:
                with LOCK:INFLIGHT-=1


def seed():
    import boto3,uuid
    from sqlalchemy import text
    from backend.app.db.models import Base,User,UserRole,Region,Dataset,TrainingRun,TrainingRunStatus,ModelVersion
    from backend.app.db.training_execution import TrainingExecution
    from backend.app.db.session import engine,SessionLocal
    Base.metadata.create_all(engine)
    with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as c:
        c.execute(text('CREATE DATABASE ci_airflow'))
    with engine.begin() as c:
        c.execute(text('CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) NOT NULL)'))
        c.execute(text("INSERT INTO alembic_version VALUES ('20261028_01')"))
    with SessionLocal() as db:
        user=User(email='ci-only@example.test',password_hash='not-a-login',role=UserRole.ADMIN);db.add(user);db.flush()
        region=Region(name='CI synthetic region',latitude=1,longitude=1,timezone='UTC');db.add(region);db.flush()
        dataset=Dataset(region_id=region.id,contract_version='traffic-training-csv/v1',original_filename='synthetic.csv',
                        storage_uri='s3://ci-runtime/data/ci-input.npy',sha256='0'*64,uploaded_by=user.id)
        db.add(dataset);db.flush()
        run=TrainingRun(region_id=region.id,dataset_id=dataset.id,split_policy='ci-only',requested_by=user.id,status=TrainingRunStatus.COMPLETED)
        db.add(run);db.flush()
        model=ModelVersion(region_id=region.id,training_run_id=run.id,variant='RandomForest',version='ci-v1',
                           mlflow_run_id='ci-only',mlflow_model_uri='/app/models/ci-probe.joblib',artifact_uri='/app/models/ci-probe.joblib',
                           cv_mean_mae=0,cv_std_mae=0,final_test_mae=0)
        db.add(model);db.flush();region.active_model_version_id=model.id;db.commit()
    client=boto3.client('s3',endpoint_url='http://minio:9000',aws_access_key_id=os.environ['MINIO_ROOT_USER'],
                        aws_secret_access_key=os.environ['MINIO_ROOT_PASSWORD'],region_name='us-east-1')
    for bucket in ('ci-runtime','mlflow-artifacts'):client.create_bucket(Bucket=bucket)
    for source,key in [('random_forest.joblib','models/ci-probe.joblib'),('tree-input.npy','data/ci-input.npy')]:
        client.put_object(Bucket='ci-runtime',Key=key,Body=(Path('/fixtures')/source).read_bytes())
    print(json.dumps({'synthetic_database_and_storage_ready':True,'provider_called':False}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['role','airflow','health','proxy','s3','seed']);p.add_argument('--port',type=int,default=8000)
    args=p.parse_args();MODE=args.mode
    if MODE=='seed':seed()
    else:
        if MODE=='role' and os.environ.get('REGION_CLEANUP_ROLE')=='api':inference()
        server=ThreadingHTTPServer(('0.0.0.0',args.port),Handler)
        if MODE=='proxy':
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain('/fixtures/server.crt','/fixtures/server.key')
            server.socket=context.wrap_socket(server.socket,server_side=True)
        server.serve_forever()
