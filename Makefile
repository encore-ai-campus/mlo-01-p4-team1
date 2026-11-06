PYTHON ?= .venv/bin/python

.PHONY: prep-check prep-test foundation-unit foundation-integration runtime-unit runtime-integration integration-contract integration-execution lifecycle-unit lifecycle-integration integration-lifecycle preauth-check predeploy-check

# Ordered acceptance: Foundation mock exports precede the common graph checks;
# Docker suites run serially even when the caller supplies make -j.
.NOTPARALLEL: predeploy-check

prep-check:
	$(PYTHON) scripts/check_preparation.py

prep-test:
	$(PYTHON) -m unittest discover -s tests/preparation -p 'test_*.py' -v

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

predeploy-check: prep-check prep-test foundation-unit foundation-integration runtime-unit runtime-integration integration-contract integration-execution lifecycle-unit lifecycle-integration integration-lifecycle preauth-check
