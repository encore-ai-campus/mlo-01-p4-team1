import json
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker
from backend.recommendation.errors import require

CONTRACT = json.loads((Path(__file__).with_name('contract.json')).read_text())
SCHEMAS = CONTRACT['components']['schemas']


def validate(name, body):
    require(isinstance(body, dict), 'INVALID_REQUEST', 'JSON 객체를 입력해 주세요.', 400)
    root = {'$ref':'#/components/schemas/'+name, 'components':CONTRACT['components']}
    validator = Draft202012Validator(root, format_checker=FormatChecker())
    require(not next(validator.iter_errors(body), None), 'INVALID_REQUEST', '입력 항목과 형식을 확인해 주세요.', 400)
    return body
