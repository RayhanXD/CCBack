#!/usr/bin/env python3

import requests
import json
import sys

def test_endpoint(url, method='GET', data=None, expected_status=200):
    """Test an API endpoint"""
    try:
        if method == 'GET':
            response = requests.get(url)
        elif method == 'POST':
            response = requests.post(url, json=data, headers={'Content-Type': 'application/json'})
        elif method == 'PUT':
            response = requests.put(url, json=data, headers={'Content-Type': 'application/json'})
        
        print(f"\n{method} {url}")
        print(f"Status Code: {response.status_code}")
        print(f"Response: {response.text[:200]}...")
        
        if response.status_code == expected_status:
            print("✅ Test PASSED")
        else:
            print("❌ Test FAILED")
        
        return response.status_code == expected_status
        
    except Exception as e:
        print(f"\n{method} {url}")
        print(f"❌ Test FAILED - Exception: {str(e)}")
        return False

def main():
    base_url = "http://localhost:8000"
    
    print("🧪 Testing Campus Connect API Endpoints")
    print("=" * 50)
    
    # Test health endpoint
    test_endpoint(f"{base_url}/health")
    
    # Test majors endpoint
    test_endpoint(f"{base_url}/majors")
    
    # Test categories endpoint
    test_endpoint(f"{base_url}/categories")
    
    # Test major colors endpoint
    test_endpoint(f"{base_url}/major-colors")
    
    # Test signup endpoint (JSON)
    signup_data = {
        "name": "Test User",
        "surname": "Test",
        "school_name": "University of Texas at Dallas",
        "year": "2",
        "ftcs_status": "No",
        "gpa_range": "3.0 - 3.5",
        "educational_goals": "Graduate with honors",
        "age": "20",
        "gender": "Prefer not to say",
        "race_ethnicity": "Prefer not to say",
        "working_hours": "0-10",
        "stress_level": "Moderate",
        "self_efficacy": "High",
        "major": "Computer Science",
        "interests": ["Technology", "Programming"],
        "email": "test@utdallas.edu"
    }
    test_endpoint(f"{base_url}/signup", method='POST', data=signup_data, expected_status=201)
    
    # Test signin endpoint
    signin_data = {"email": "test@utdallas.edu"}
    test_endpoint(f"{base_url}/signin", method='POST', data=signin_data)
    
    print("\n" + "=" * 50)
    print("🏁 Testing completed!")

if __name__ == "__main__":
    main()
