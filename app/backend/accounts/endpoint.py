import json
from backend.recommendation.api import Endpoint as BaseEndpoint
from backend.recommendation.errors import require


class Endpoint(BaseEndpoint):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if request.method in ('POST', 'PATCH', 'DELETE'):
            require(isinstance(request.data, dict), 'INVALID_REQUEST', '입력 형식을 확인해 주세요.')
            require(len(json.dumps(request.data)) <= 65536, 'INVALID_REQUEST', '입력 크기를 확인해 주세요.')
