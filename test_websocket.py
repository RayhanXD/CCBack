#!/usr/bin/env python3
"""
Test script for the WebSocket API
"""
import asyncio
import websockets
import json

async def test_websocket():
    """Test the WebSocket API"""
    uri = "ws://localhost:8000/ws/chatgpt/test@example.com"
    
    print(f"Connecting to {uri}...")
    async with websockets.connect(uri) as websocket:
        print("Connected!")
        
        # Create a test message
        message = {
            "system": "You are a helpful assistant for Campus Connect.",
            "message": "Hello, can you tell me about the weather today?",
            "model": "gpt-4o-mini",
            "temperature": 0.7,
            "max_tokens": 100
        }
        
        # Send the message
        print("Sending message...")
        await websocket.send(json.dumps(message))
        
        # Receive the response
        print("Waiting for response...")
        full_response = ""
        last_message_time = asyncio.get_event_loop().time()
        timeout_duration = 5.0  # Consider streaming complete after 5 seconds of no messages
        
        while True:
            try:
                # Use a shorter timeout for each message
                response = await asyncio.wait_for(websocket.recv(), timeout=2.0)
                print(f"Received: {response}")
                full_response += response
                last_message_time = asyncio.get_event_loop().time()  # Update the last message time
            except asyncio.TimeoutError:
                # Check if we've waited long enough since the last message
                current_time = asyncio.get_event_loop().time()
                if current_time - last_message_time >= timeout_duration:
                    print("No messages received for a while, assuming streaming is complete")
                    break
                # Otherwise, continue waiting
                continue
        
        print("\nFull response:")
        print(full_response)

if __name__ == "__main__":
    asyncio.run(test_websocket())
