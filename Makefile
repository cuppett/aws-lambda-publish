.PHONY: install lint test build deploy

install:
	python -m pip install -r requirements.txt

lint:
	echo "No linter configured yet"

test:
	pytest -q

build:
	sam build --use-container

deploy:
	sam deploy --stack-name lambda-publish-core --capabilities CAPABILITY_NAMED_IAM
