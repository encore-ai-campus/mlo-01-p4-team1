PYTHON ?= .venv/bin/python
APP_IMAGES ?= .artifacts/app-predeploy/images.json
APP_CHECK_OUTPUT ?= .artifacts/app-predeploy/summary.json

.PHONY: prep-check prep-test foundation-unit foundation-integration runtime-unit runtime-integration integration-contract integration-execution lifecycle-unit lifecycle-integration integration-lifecycle preauth-check predeploy-check app-images app-predeploy

# Ordered acceptance: Foundation mock exports precede the common graph checks;
# Docker suites run serially even when the caller supplies make -j.
.NOTPARALLEL: predeploy-check

prep-check:
	$(PYTHON) scripts/check_preparation.py

prep-test:
	$(PYTHON) scripts/required_suite.py --start tests/preparation --pattern 'test_*.py'

foundation-unit:
	@test -f aws-infra/foundation/scripts/check-local.sh || { echo 'NOT READY: A의 check-local.sh가 아직 없습니다.'; exit 2; }
	bash aws-infra/foundation/scripts/check-local.sh unit

foundation-integration:
	@test -f aws-infra/foundation/scripts/check-local.sh || { echo 'NOT READY: A의 check-local.sh가 아직 없습니다.'; exit 2; }
	bash aws-infra/foundation/scripts/check-local.sh integration

runtime-unit:
	@test -f aws-infra/runtime/scripts/check-local.sh || { echo 'NOT READY: B의 check-local.sh가 아직 없습니다.'; exit 2; }
	bash aws-infra/runtime/scripts/check-local.sh unit

runtime-integration:
	@test -f aws-infra/runtime/scripts/check-local.sh || { echo 'NOT READY: B의 check-local.sh가 아직 없습니다.'; exit 2; }
	bash aws-infra/runtime/scripts/check-local.sh integration

integration-contract:
	$(PYTHON) scripts/check_preparation.py --pipeline

integration-execution:
	$(PYTHON) scripts/check_execution_contract.py

lifecycle-unit:
	bash aws-infra/lifecycle/scripts/check-local.sh unit

lifecycle-integration:
	bash aws-infra/lifecycle/scripts/check-local.sh integration

integration-lifecycle:
	aws-infra/runtime/.venv/bin/python scripts/check_lifecycle_contract.py

preauth-check:
	aws-infra/runtime/.venv/bin/python scripts/check_preauth.py

app-images:
	$(PYTHON) scripts/build_app_images.py --output "$(APP_IMAGES)"

app-predeploy:
	@test -f "$(APP_IMAGES)" || { echo 'NOT READY: run make app-images for the current source first.'; exit 2; }
	aws-infra/runtime/.venv/bin/python scripts/check_app_predeploy.py --images "$(APP_IMAGES)" --output "$(APP_CHECK_OUTPUT)"

predeploy-check: prep-check prep-test foundation-unit foundation-integration runtime-unit runtime-integration integration-contract integration-execution lifecycle-unit lifecycle-integration integration-lifecycle preauth-check
