"""
Demo script showing how to use the Course Learning Agent system.

This script demonstrates:
1. Creating a workspace
2. Ingesting documents
3. Building RAG index
4. Using different modes (Learn, Practice, Exam)
"""
import os
import sys

# Note: This is a demonstration script. To actually run it:
# 1. Install dependencies: pip install -r requirements.txt
# 2. Configure .env with your API key
# 3. Run: python examples/demo.py


def demo_workflow():
    """Demonstrate the complete workflow."""
    
    print("=" * 70)
    print("Course Learning Agent - Demo Workflow")
    print("=" * 70)
    print()
    
    # Step 1: Setup
    print("馃搵 Step 1: Initial Setup")
    print("-" * 70)
    print("1. Clone the repository")
    print("2. Install dependencies: pip install -r requirements.txt")
    print("3. Configure .env file with your OPENAI_API_KEY")
    print()
    
    # Step 2: Start services
    print("馃殌 Step 2: Start Services")
    print("-" * 70)
    print("Terminal 1: python backend/api.py")
    print("Terminal 2: streamlit run frontend/streamlit_app.py")
    print()
    
    # Step 3: Create workspace
    print("馃摎 Step 3: Create Course Workspace")
    print("-" * 70)
    print("In the Streamlit UI:")
    print("  1. Click '鉃?鍒涘缓鏂拌绋?")
    print("  2. Enter course name: '绾挎€т唬鏁?")
    print("  3. Enter subject: '鏁板'")
    print("  4. Click '鍒涘缓'")
    print()
    
    # Step 4: Upload documents
    print("馃搫 Step 4: Upload Course Materials")
    print("-" * 70)
    print("Upload sample documents:")
    print("  - tests/sample_textbook.txt (provided)")
    print("  - Your own PDF/TXT/MD files")
    print()
    print("Then click '馃敤 鏋勫缓绱㈠紩' to build the RAG index")
    print()
    
    # Step 5: Learn Mode
    print("馃摉 Step 5: Use Learn Mode")
    print("-" * 70)
    print("Example queries:")
    print("  鉁?'浠€涔堟槸鐭╅樀鐨勭З锛?")
    print("  鉁?'瑙ｉ噴绾挎€х浉鍏冲拰绾挎€ф棤鍏?")
    print("  鉁?'濡備綍璁＄畻鐭╅樀鐨勭З锛?")
    print()
    print("Expected output:")
    print("  - Structured answer with definitions")
    print("  - Citations from textbook with page numbers")
    print("  - Key points and common mistakes")
    print()
    
    # Step 6: Practice Mode
    print("鉁嶏笍 Step 6: Use Practice Mode")
    print("-" * 70)
    print("Example workflow:")
    print("  1. User: '缁欐垜鍑轰竴閬撳叧浜庣煩闃电З鐨勪腑绛夐毦搴︾粌涔犻'")
    print("  2. System: [Generates question with rubric]")
    print("  3. User: [Submits answer]")
    print("  4. System: [Provides score, feedback, and mistake analysis]")
    print()
    print("Mistakes are automatically saved to:")
    print("  data/workspaces/<course>/mistakes/mistakes.jsonl")
    print()
    
    # Step 7: Exam Mode
    print("馃摑 Step 7: Use Exam Mode")
    print("-" * 70)
    print("Example workflow:")
    print("  1. Switch to 'Exam Mode' in sidebar")
    print("  2. User: '寮€濮嬬嚎鎬т唬鏁扮涓€绔犳祴璇?")
    print("  3. System: [Generates exam question]")
    print("     Note: WebSearch is disabled in this mode")
    print("  4. User: [Submits answer]")
    print("  5. System: [Provides grade and report]")
    print()
    
    # Step 8: Review
    print("馃搳 Step 8: Review and Analyze")
    print("-" * 70)
    print("Check your progress:")
    print("  - View mistake log: data/workspaces/<course>/mistakes/")
    print("  - Review notes: data/workspaces/<course>/notes/")
    print("  - Analyze weak topics from exam reports")
    print()
    
    print("=" * 70)
    print("鉁?Demo workflow complete!")
    print("=" * 70)
    print()
    print("馃挕 Tips:")
    print("  - Use specific terminology for better RAG retrieval")
    print("  - Each mode has different tool permissions")
    print("  - All answers include textbook citations")
    print("  - Practice mode builds a mistake log automatically")
    print()


def show_api_examples():
    """Show API usage examples."""
    print()
    print("=" * 70)
    print("API Usage Examples")
    print("=" * 70)
    print()
    
    print("1锔忊儯 Create Workspace:")
    print("-" * 70)
    print("""
POST http://localhost:8000/workspaces
Content-Type: application/json

{
    "course_name": "绾挎€т唬鏁?,
    "subject": "鏁板"
}
""")
    
    print("2锔忊儯 Upload Document:")
    print("-" * 70)
    print("""
POST http://localhost:8000/workspaces/绾挎€т唬鏁?upload
Content-Type: multipart/form-data

file: <your_file.pdf>
""")
    
    print("3锔忊儯 Build Index:")
    print("-" * 70)
    print("""
POST http://localhost:8000/workspaces/绾挎€т唬鏁?build-index
""")
    
    print("4锔忊儯 Chat (Learn Mode):")
    print("-" * 70)
    print("""
POST http://localhost:8000/chat
Content-Type: application/json

{
    "course_name": "绾挎€т唬鏁?,
    "mode": "learn",
    "message": "浠€涔堟槸鐭╅樀鐨勭З锛?,
    "history": []
}

Response:
{
    "message": {
        "role": "assistant",
        "content": "[Structured teaching content]",
        "citations": [
            {
                "text": "鐭╅樀鐨勭З瀹氫箟涓?..",
                "doc_id": "sample_textbook.txt",
                "page": null,
                "score": 0.85
            }
        ]
    },
    "plan": {
        "need_rag": true,
        "allowed_tools": ["calculator", "websearch", "filewriter"],
        "task_type": "learn"
    }
}
""")


def show_architecture():
    """Show system architecture."""
    print()
    print("=" * 70)
    print("System Architecture Overview")
    print("=" * 70)
    print()
    print("""
鈹屸攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹?
鈹? Streamlit  鈹? Frontend UI (port 8501)
鈹?  Frontend  鈹? - Course selection
鈹斺攢鈹€鈹€鈹€鈹€鈹€鈹攢鈹€鈹€鈹€鈹€鈹€鈹? - Mode switching
       鈹?        - Chat interface
       鈹?HTTP
鈹屸攢鈹€鈹€鈹€鈹€鈹€鈻尖攢鈹€鈹€鈹€鈹€鈹€鈹?
鈹?  FastAPI   鈹? Backend API (port 8000)
鈹?  Backend   鈹? - Workspace management
鈹斺攢鈹€鈹€鈹€鈹€鈹€鈹攢鈹€鈹€鈹€鈹€鈹€鈹? - Document upload
       鈹?        - Chat endpoint
       鈹?
鈹屸攢鈹€鈹€鈹€鈹€鈹€鈻尖攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹?
鈹? Orchestration Runner   鈹? Core orchestration
鈹?                        鈹?
鈹? 鈹屸攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹?  鈹?
鈹? 鈹? Router Agent   鈹?  鈹? Planning
鈹? 鈹斺攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹?  鈹?
鈹?          鈹?           鈹?
鈹? 鈹屸攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈻尖攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹?  鈹?
鈹? 鈹? Tutor Agent    鈹?  鈹? Teaching (Learn mode)
鈹? 鈹? QuizMaster     鈹?  鈹? Question gen (Practice/Exam)
鈹? 鈹? Grader Agent   鈹?  鈹? Evaluation (Practice/Exam)
鈹? 鈹斺攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹?  鈹?
鈹斺攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹尖攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹?
            鈹?
    鈹屸攢鈹€鈹€鈹€鈹€鈹€鈹€鈹尖攢鈹€鈹€鈹€鈹€鈹€鈹€鈹?
    鈹?      鈹?      鈹?
鈹屸攢鈹€鈹€鈻尖攢鈹€鈹€鈹?鈹屸攢鈻尖攢鈹€鈹?鈹屸攢鈻尖攢鈹€鈹€鈹€鈹€鈹?
鈹? RAG  鈹?鈹侻CP 鈹?鈹侽utput 鈹?
鈹係ystem 鈹?鈹俆ool鈹?鈹侳ormat 鈹?
鈹斺攢鈹€鈹€鈹€鈹€鈹€鈹€鈹?鈹斺攢鈹€鈹€鈹€鈹?鈹斺攢鈹€鈹€鈹€鈹€鈹€鈹€鈹?

Key Components:
- RAG: Document parsing, chunking, embedding, retrieval
- MCP: Calculator, WebSearch, FileWriter tools
- Agents: Router, Tutor, QuizMaster, Grader
- Policy: Tool permission control per mode
""")


if __name__ == "__main__":
    demo_workflow()
    
    if "--api" in sys.argv:
        show_api_examples()
    
    if "--arch" in sys.argv:
        show_architecture()
    
    print()
    print("馃捇 For detailed documentation, see:")
    print("   - README.md: Overview and quick start")
    print("   - docs/USAGE.md: Detailed usage examples")
    print("   - docs/ARCHITECTURE.md: System design details")
    print()

