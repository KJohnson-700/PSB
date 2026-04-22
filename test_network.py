import requests

url = "https://graphql.polymarket.com/"

print(f"Attempting to connect to {url}...")

try:
    response = requests.get(url, timeout=10)
    print(f"Success! Received status code: {response.status_code}")
    print("Response content (first 150 chars):")
    print(response.text[:150])
except requests.exceptions.Timeout:
    print("\nError: Connection timed out.")
    print("This means the server at the specified URL did not respond within the timeout period (10 seconds).")
except requests.exceptions.ConnectionError as e:
    print(f"\nError: Connection failed.")
    print("This indicates a network problem, such as a DNS failure, refused connection, or firewall block.")
    print(f"Details: {e}")
except Exception as e:
    print(f"\nAn unexpected error occurred: {e}")
