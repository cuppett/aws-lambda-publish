import json

def lambda_handler(event, context):
    return {
        'statusCode': 200,
        'body': json.dumps({
            'message': 'Hello from test Lambda function!',
            'version': '2.0'
        })
    }