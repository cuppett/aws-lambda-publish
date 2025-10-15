#!/bin/bash

# Create a new test image with a different tag to trigger real updates
export AWS_PROFILE=cuppett
export AWS_REGION=us-west-2

echo "Creating a modified test image for direct mode testing..."

# Create a simple modification to the image to get a different digest
cat > /tmp/Dockerfile.test << 'EOF'
FROM 771294529343.dkr.ecr.us-west-2.amazonaws.com/lambda-publish-test:v1.0
ENV TEST_UPDATE=true
LABEL updated=$(date)
EOF

# Build new image
podman build -t 771294529343.dkr.ecr.us-west-2.amazonaws.com/lambda-publish-test:direct-test-v2 -f /tmp/Dockerfile.test /tmp/

# Push the new image
echo "Pushing direct-test-v2 tag..."
podman push 771294529343.dkr.ecr.us-west-2.amazonaws.com/lambda-publish-test:direct-test-v2

echo "New image pushed successfully!"