.PHONY: install lint test build-artifact package deploy subscribe-ddns clean

PROFILE ?= cuppett
REGION ?= us-east-1
SAM ?= sam
STACK ?= lambda-publish-core
ARTIFACT_BUCKET ?= fedora-builds-pipelinebucket-uueu3sxbscop
PYTHON ?= python3

install:
	$(PYTHON) -m pip install -r requirements.txt

lint:
	echo "No linter configured yet"

test:
	pytest -q

build-artifact:
	rm -rf build/lambda
	mkdir -p build/lambda
	$(PYTHON) -m pip install -q -r requirements.txt -t build/lambda
	cp -r src build/lambda/

build: build-artifact

package: build-artifact
	aws cloudformation package \
		--profile $(PROFILE) \
		--region $(REGION) \
		--template-file template.yaml \
		--s3-bucket $(ARTIFACT_BUCKET) \
		--s3-prefix lambda-publish \
		--output-template-file packaged.yaml

deploy: package
	aws cloudformation deploy \
		--profile $(PROFILE) \
		--region $(REGION) \
		--template-file packaged.yaml \
		--stack-name $(STACK) \
		--capabilities CAPABILITY_NAMED_IAM \
		--no-fail-on-empty-changeset

subscribe-ddns:
	PROFILE=$(PROFILE) REGION=$(REGION) ./scripts/subscribe_ddns.sh

clean:
	rm -rf build packaged.yaml .aws-sam .pytest_cache __pycache__
