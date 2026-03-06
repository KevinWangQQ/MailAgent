from dotenv import load_dotenv
load_dotenv()
import uvicorn
uvicorn.run("app:app", host="127.0.0.1", port=8100)
