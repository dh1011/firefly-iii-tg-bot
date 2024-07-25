import os
import requests
import uuid
import json
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Get environment variables
url = os.getenv('FF_API_ENDPOINT')
auth_token = os.getenv('FF_API_TOKEN')

def enter_transaction(trans_datetime, amount, description, category_name, source_name):
    # Generate a unique X-Trace-Id
    x_trace_id = str(uuid.uuid4())

    # Headers
    headers = {
        'Authorization': f'Bearer {auth_token}',
        'X-Trace-Id': x_trace_id,
        'Content-Type': 'application/json'
    }

    params = {
        "transactions": [
            {
            "type": "withdrawal",
            "date": trans_datetime,
            "amount": amount,
            "description": description,
            "category_name": category_name,
            "source_name": source_name,
            }
        ]
    }

    # Sending the GET request
    return requests.post(url, headers=headers, json=params)
