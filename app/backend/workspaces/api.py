"""Default-deny platform routes. No arbitrary handler names are accepted from clients."""
import json
import uuid
from django.db import IntegrityError
from rest_framework.response import Response
from rest_framework.exceptions import APIException
from backend.recommendation.api import Endpoint
from backend.accounts import services as account_services
from backend.recommendation.errors import DomainError, require
from . import accounts, service, authorization as auth
from .schema import validate, CONTRACT

ACCOUNT_OPERATIONS={
    'getPolicies':'policies','checkAvailability':'availability','signup':'signup','login':'login',
    'logoutAll':'logout_all','currentSession':'current','logout':'logout','getMe':'me','patchMe':'profile',
    'addMembership':'add_membership','setContext':'context','enterService':'entry',
    'requestEmail':'email_add','removeEmail':'email_remove','verifyEmail':'email_verify',
    'changePassword':'password','requestRecovery':'recovery_request','completeRecovery':'recovery_complete','withdraw':'withdraw',
}


class PlatformEndpoint(Endpoint):
    operations=None

    def initial(self,request,*args,**kwargs):
        super().initial(request,*args,**kwargs)
        auth.enabled()
        require(account_services.limit('platform-ip:'+request.META.get('REMOTE_ADDR',''),maximum=2000),
                'RATE_LIMITED','요청이 많습니다. 잠시 후 다시 시도해 주세요.',429)

    def handle_exception(self,exc):
        if isinstance(exc,DomainError):
            response=Response({'error':{'code':exc.code,'message':exc.message},'request_id':getattr(self.request,'request_id',str(uuid.uuid4()))},status=400 if exc.status==422 else exc.status)
            return response
        if isinstance(exc,IntegrityError):
            return Response({'error':{'code':'STATE_CONFLICT','message':'동시에 변경되었거나 중복된 대상입니다. 새로 확인해 주세요.'},
                'request_id':getattr(self.request,'request_id',str(uuid.uuid4()))},status=409)
        if isinstance(exc,APIException):
            return Response({'error':{'code':'REQUEST_FAILED','message':'요청 형식을 확인해 주세요.'},
                'request_id':getattr(self.request,'request_id',str(uuid.uuid4()))},status=exc.status_code)
        return super().handle_exception(exc)

    def execute(self,request,**values):
        descriptor=self.operations.get(request.method.lower())
        require(descriptor is not None,'METHOD_NOT_ALLOWED','허용되지 않는 요청입니다.',405)
        operation=descriptor['operationId']
        if request.method not in ('GET','HEAD'):
            require(not request.query_params,'INVALID_REQUEST','변경 요청의 query parameter는 허용하지 않습니다.',400)
            require(int(request.META.get('CONTENT_LENGTH') or 0)<=65536,'BODY_TOO_LARGE','본문 크기를 확인해 주세요.',413)
        body=request.data if request.method not in ('GET','HEAD') else {}
        schema=descriptor.get('requestBody',{}).get('content',{}).get('application/json',{}).get('schema',{}).get('$ref')
        if schema:
            # An unconsented email is discarded before address validation or persistence.
            if operation=='signup' and isinstance(body,dict) and body.get('email_consent') is False:
                body={k:v for k,v in body.items() if k!='email'}
            validate(schema.rsplit('/',1)[-1],body)
        else:
            require(body in ({},None),'INVALID_REQUEST','이 요청은 본문 입력을 받지 않습니다.',400)
            body={}
        if operation in ACCOUNT_OPERATIONS:
            data,status=accounts.execute(ACCOUNT_OPERATIONS[operation],request,body)
        elif operation in ('issueInvitationReceiver','reauthenticateIntent'):
            data,status=service.secret(operation,request,body)
        elif request.method=='GET':
            data,status=service.read(operation,request,values),200
        else:
            data,status=service.write(operation,request,body,values)
        response=Response(status=204) if status==204 else Response({'data':data,'request_id':request.request_id},status=status)
        if isinstance(data,dict) and type(data.get('revision')) is int:
            response['ETag']='"'+str(data['revision'])+'"'
        return response

    def get(self,request,**values): return self.execute(request,**values)
    def post(self,request,**values): return self.execute(request,**values)
    def patch(self,request,**values): return self.execute(request,**values)
    def put(self,request,**values): return self.execute(request,**values)
    def delete(self,request,**values): return self.execute(request,**values)


def openapi(request):
    from django.http import JsonResponse
    auth.enabled()
    return JsonResponse(CONTRACT)
