#!/usr/bin/env python3
"""
Test script to verify API endpoints are working
"""
import requests
import json
import sys
from datetime import datetime

API_BASE_URL = "http://localhost:8000"

def test_endpoint(endpoint, method="GET", data=None, expected_status=200):
    """Test a single API endpoint"""
    url = f"{API_BASE_URL}{endpoint}"
    
    try:
        if method == "GET":
            response = requests.get(url, timeout=10)
        elif method == "POST":
            response = requests.post(url, json=data, timeout=10)
        else:
            print(f"❌ Unsupported method: {method}")
            return False
        
        if response.status_code == expected_status:
            print(f"✅ {method} {endpoint} - Status: {response.status_code}")
            return True
        else:
            print(f"❌ {method} {endpoint} - Expected: {expected_status}, Got: {response.status_code}")
            if response.text:
                print(f"   Response: {response.text[:200]}...")
            return False
            
    except requests.exceptions.ConnectionError:
        print(f"❌ {method} {endpoint} - Connection failed (is the server running?)")
        return False
    except requests.exceptions.Timeout:
        print(f"❌ {method} {endpoint} - Request timeout")
        return False
    except Exception as e:
        print(f"❌ {method} {endpoint} - Error: {str(e)}")
        return False

def main():
    print("🧪 Testing Campus Connect API Endpoints")
    print("=" * 50)
    
    # Test basic endpoints
    tests = [
        ("/", "GET"),
        ("/health", "GET"),
        ("/majors", "GET"),
        ("/categories", "GET"),
        ("/major-colors", "GET"),
    ]
    
    passed = 0
    total = len(tests)
    
    for endpoint, method in tests:
        if test_endpoint(endpoint, method):
            passed += 1
    
    print("\n" + "=" * 50)
    print(f"📊 Test Results: {passed}/{total} passed")
    
    if passed == total:
        print("🎉 All tests passed! API is working correctly.")
        return 0
    else:
        print("⚠️  Some tests failed. Check the server logs.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
