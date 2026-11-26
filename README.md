# AI Jobs Interview Assistant

## Overview

AI Jobs Interview Assistant is a comprehensive, production-quality application designed to help students and job seekers prepare for real-world job interviews. Built as a Final Year University Project, it demonstrates expertise in Generative AI, LLMs, RAG, Vector Databases, Embeddings, Prompt Engineering, Speech-to-Text, and modern software engineering practices.

The application analyzes resumes, matches them against job descriptions, generates personalized interview questions, conducts mock interviews with AI evaluation, and provides detailed performance reports with improvement plans.

## Features

### 📄 Resume Analysis

- Upload PDF, DOCX, or TXT resumes
- Extract structured information: education, experience, skills, projects, certifications
- AI-powered scoring across 6 categories (Technical Skills, Experience, Projects, Education, Certifications, Structure)
- Contact information extraction (email, phone, LinkedIn, GitHub, portfolio)

### 🎯 Job Description Matching

- Paste or upload job descriptions
- Extract requirements: skills, experience, education, responsibilities, keywords
- Compare resume vs job description with detailed match scores
- Identify matching skills, missing skills, skill gaps, and experience gaps
- Actionable recommendations for improvement

### 🤖 Interview Question Generation

- 7 question categories: Technical, HR, Behavioral, Situational, Project-based, Resume-based, Job-specific
- 3 difficulty levels: Easy, Medium, Hard
- Personalized questions based on resume, job description, and skill gaps
- Dynamic follow-up question generation based on answers

### 🎤 Mock Interview Engine

- Interactive interview sessions with real-time AI evaluation
- Text and voice answer modes
- Voice answers transcribed using local Whisper (no paid APIs)
- Immediate feedback after each answer

### 📊 Answer Evaluation

- 5-dimensional scoring (0-10): Technical Accuracy, Relevance, Communication, Confidence, Completeness
- Detailed feedback: strengths, weaknesses, missing points, incorrect points
- Suggested better answers
- Communication and technical feedback

### 📈 Interview Reports

- Overall, Technical, Communication, HR, and Problem-Solving scores
- Strengths and weaknesses analysis
- Frequently missed concepts
- Recommended topics, projects, and practice questions
- 7-day and 30-day improvement plans

### 📊 Dashboard & Progress Tracking

- Resume score, job match score, latest interview score
- Interview history with score trends derived from persisted sessions
- Category-level performance and focus areas
- Progress charts over time

### 🔐 Accounts & Workspaces

- Sign-up, email login, role-based admin, and rate-limited authentication
- Stable workspace tokens with owner-scoped queries for all saved data
- Per-user preferences persisted in the database

### 📚 Knowledge Base (RAG)

- Per-workspace document upload, chunking, embeddings, and FAISS indexing
- Cited, grounded answers and question generation from your documents
- Atomic index persistence with embedding-model invalidation

### 📄 Report Export

- Download interview reports as JSON or PDF
- PDF files are generated with ReportLab and persisted per workspace

## Architecture

```text
ai-jobs-interview-assistant/
├── app.py                       # Streamlit entry point and navigation registry
├── backend/
│   ├── config/settings.py       # Environment-driven settings
│   ├── database/                # SQLAlchemy engine, models, additive migrations
│   ├── rag/                     # Loaders, splitters, embeddings, FAISS, retrieval
│   ├── services/                # Auth, LLM, RAG, analysis, reports, roadmap services
│   └── utils/                   # File validation and text helpers
├── frontend/
│   ├── shared/                  # Auth UI, components, password change, legacy navigation
│   ├── admin/                   # Admin-only pages
│   └── user/                    # Live user pages plus legacy wrappers/modules
├── docs/                        # Provider and fallback documentation
├── data/                        # Gitignored database, uploads, vectors, cache
├── .streamlit/                  # Theme and server configuration
├── pyproject.toml               # Tooling and canonical optional dev dependencies
├── requirements.txt             # Runtime dependency list
├── .env.example                 # Secrets-free environment variable template
├── README.md                    # This file
├── reports/                     # Local phase audit and verification reports (gitignored)
└── tests/                       # Local test suite (gitignored; not published)
```

## Tech Stack

| Component | Technology | Purpose |
|-----------|------------|---------|
| **LLM** | Groq / Gemini / Ollama | Centralized provider manager with automatic fallback |
| **Embeddings** | Sentence Transformers (all-MiniLM-L6-v2) | Local text embeddings |
| **Vector DB** | FAISS | Local vector similarity search |
| **Speech-to-Text** | Faster-Whisper | Local audio transcription |
| **Frontend** | Streamlit | Web UI framework |
| **Database** | SQLite + SQLAlchemy | Local data persistence |
| **Document Processing** | PyPDF, python-docx | PDF/DOCX text extraction |
| **Text Splitting** | LangChain Text Splitters | Document chunking |
| **Validation** | Pydantic | Structured output validation |
| **Testing** | pytest | Unit testing |
| **Code Quality** | Ruff, Black, mypy | Linting, formatting, type checking |

## Project Structure

See the Architecture section above for the complete directory structure.

## Installation

### Prerequisites

- Python 3.11+
- Git
- (Optional) FFmpeg for audio processing

### Windows PowerShell

```powershell
# Clone the repository
git clone <your-repo-url>
cd ai-jobs-interview-assistant

# Create virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install runtime dependencies
pip install -r requirements.txt

# Install test and quality-check dependencies
pip install pytest pytest-cov ruff black mypy av

# Copy environment template
Copy-Item .env.example .env

# Edit .env with your API keys
notepad .env

# Run the application
python -m streamlit run app.py
```

### Using uv (faster)

Windows PowerShell:

```powershell
# Install uv if not available
pip install uv

# Create environment and install
uv venv
.\.venv\Scripts\Activate.ps1
uv pip install -r requirements.txt
uv pip install pytest pytest-cov ruff black mypy av
```

Linux/macOS:

```bash
# Install uv if not available
pip install uv

# Create environment and install
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
uv pip install pytest pytest-cov ruff black mypy av
```

### Linux/macOS

```bash
git clone <your-repo-url>
cd ai-jobs-interview-assistant
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-cov ruff black mypy av
cp .env.example .env
# Edit .env with your API keys
python -m streamlit run app.py
```

## Python Version

- **Declared minimum:** Python 3.11 (matches Ruff, mypy, and Black targets)
- **Verified here:** Python 3.11.2 only

## Virtual Environment

Always use a virtual environment to avoid dependency conflicts:

Create the environment:

```bash
python -m venv .venv
```

Activate it in Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Activate it in Linux/macOS:

```bash
source .venv/bin/activate
```

Deactivate it:

```bash
deactivate
```

## Environment Variables

Copy `.env.example` to `.env` and configure. See `docs/AI_PROVIDER_SETUP.md` and
`docs/AI_FALLBACK_ARCHITECTURE.md` for provider routing details.

Use `.env.example` as the complete, secrets-free variable contract. The most important groups are:

```bash
# Provider routing
AI_PROVIDER_MODE=auto
AI_PRIMARY_PROVIDER=groq
AI_FALLBACK_ENABLED=true
LLM_PROVIDER=auto
PROVIDER_PRIORITY=groq,gemini,ollama
LLM_MIN_REQUEST_INTERVAL=0
AI_REQUEST_TIMEOUT=60
AI_MAX_RETRIES=2

# Provider models and fallbacks
GEMINI_MODEL=gemini-3.5-flash
GEMINI_API_VERSION=v1beta
GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
GEMINI_FALLBACK_MODELS=gemini-3.5-flash
GROQ_MODEL=openai/gpt-oss-20b
ENABLE_OLLAMA=false

# Storage and analysis limits
DATABASE_PATH=./data/interview_assistant.db
VECTOR_STORE_PATH=./data/vector_store
MAX_RESUME_CHARS=8000
MAX_JD_CHARS=6000
MAX_RAG_CHUNKS=5
MAX_CONTEXT_CHARS=6000
```

Relative storage paths are resolved against the project root, so startup does not depend on the
current working directory. Groq is the primary provider with `groq -> gemini -> ollama` fallback.
Set `GROQ_API_KEY` in `.env`; use `GROQ_MODEL` to select a model available to your key. Gemini
remains the secondary provider and `GOOGLE_API_KEY` is a backward-compatible alias for
`GEMINI_API_KEY`.

### Groq API Setup (primary provider)

1. Go to the [Groq Console](https://console.groq.com/keys).
2. Create a new API key.
3. Add to `.env` as `GROQ_API_KEY`.
4. Optionally set `GROQ_MODEL` to another model available to your key.
5. Verify with `Settings -> Providers -> Check provider health` or see `docs/AI_PROVIDER_SETUP.md`.
6. Free tier and quota limits depend on the Groq account.

### Gemini API Setup (secondary provider)

1. Go to [Google AI Studio](https://aistudio.google.com/apikey).
2. Create a new API key and enable the Generative Language API.
3. Add to `.env` as `GEMINI_API_KEY`.
4. Optionally set `GEMINI_MODEL` to another model available to your key.

## Running the Application

Use the project virtual environment interpreter so Streamlit and project dependencies come from the same environment.

### Windows PowerShell

From the project root:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Optional custom port:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py --server.port 8501
```

### Linux/macOS

From the project root:

```bash
source .venv/bin/activate
python -m streamlit run app.py
```

Optional custom port:

```bash
python -m streamlit run app.py --server.port 8501
```

> **Note:** A global `streamlit` or `python` executable may point to a different interpreter (for example Anaconda), which can cause missing-package errors even when dependencies are installed in the project venv.

The app will open at [http://localhost:8501](http://localhost:8501).


## Example Workflow

1. **Start** - Open the app, you'll see the Dashboard
2. **Analyze Resume** - Go to "📄 Resume Analyzer", upload your resume (PDF/DOCX), click "Analyze"
3. **Analyze Job** - Go to "🎯 Job Matcher", paste or upload a job description, click "Analyze"
4. **Match** - Click "Match Resume with Job" to see compatibility scores and gaps
5. **Setup Interview** - Go to "🤖 Interview Setup", configure interview type/difficulty/questions, click "Generate Questions"
6. **Mock Interview** - Go to "🎤 Mock Interview", answer questions via text or voice
7. **View Report** - After completing, go to "📊 Interview Report" for detailed analysis
8. **Track Progress** - Dashboard shows your improvement over time

## Screenshots

*[Add screenshots here after running the application]*

- Dashboard with metrics and charts
- Resume analysis with scores
- Job match results with gaps
- Interview session in progress
- Answer evaluation feedback
- Final interview report
- Settings configuration

## Testing

Tests use pytest. They do not require real LLM API keys or a populated `.env`:
provider calls are monkeypatched, SQLite databases and media files use pytest's
`tmp_path`, and Streamlit pages are exercised with `AppTest`.

Run all commands from the repository root using the project virtual environment.

### Install test dependencies

```bash
python -m pip install -r requirements.txt
python -m pip install pytest pytest-cov ruff black mypy av
```

`av` is required by the live-media tests. Pytest configuration lives in
`pyproject.toml` (`testpaths = ["tests"]`, `pythonpath = [".", "backend"]`).

### Run tests

```bash
# Full suite
python -m pytest

# Verbose full suite
python -m pytest -v

# Specific test files
python -m pytest tests/test_core.py -v
python -m pytest tests/test_user_management.py tests/test_analytics_regression.py -v

# Select by keyword
python -m pytest -k "auth or analytics or provider" -v

# Stop on first failure
python -m pytest -x
```

The full suite is UI-heavy and can take several minutes. Use targeted files or
`-k` during development.

### Coverage

```bash
python -m pytest --cov=backend --cov-report=term-missing
python -m pytest --cov=backend --cov-report=html
```

### Test structure

- `tests/test_core.py`: models, helpers, settings, RAG, provider fallback, validation, and persistence
- `tests/test_user_management.py`: authentication, admin workflows, RBAC, password/reset security
- `tests/test_user_dashboard.py`: dashboard data, owner scoping, page wiring, and `AppTest` renders
- `tests/test_analytics_regression.py`: analytics calculations, filters, empty states, and owner isolation
- `tests/test_career_features.py`: resume/JD processing, validation, and PDF reports
- `tests/test_job_interviews.py`, `tests/test_interview_practice.py`, `tests/test_roadmap_generation.py`: interview and roadmap workflows
- `tests/test_live_*`, `tests/test_stage3_*`, `tests/test_stage4*`: live calls, WebRTC, recording lifecycle, media integrity, and concurrency
- `tests/test_secret_hygiene.py`: `.env.example`, `.gitignore`, and secret-handling guards

### Isolation model

- SQLite databases, vector stores, and media paths are redirected to `tmp_path`.
- Environment variables and settings are changed with `pytest.monkeypatch`.
- LLM providers are replaced with scripted or fake implementations.
- Streamlit pages are rendered through `streamlit.testing.v1.AppTest`.

Do not put real API keys in `.env` when running tests. Use `.env.example` as the
template and keep real credentials in the gitignored `.env` file only.

### Quality checks

```bash
python -m ruff check .
python -m black --check .
python -m mypy backend frontend app.py
```

## Troubleshooting

### Common Issues

**1. "ModuleNotFoundError: No module named 'faster_whisper'"**

```bash
pip install faster-whisper
```

**2. "CUDA out of memory" or Whisper slow**

- Use smaller model: `WHISPER_MODEL=tiny` or `base`
- Ensure `WHISPER_DEVICE=cpu` if no GPU

**3. "Groq API rate limit exceeded"**

- Wait and retry (automatic retry with backoff implemented)
- Check quota at the [Groq Console](https://console.groq.com/).

**4. "FAISS index not found"**

- First run creates the index automatically
- Check `data/vector_store/` permissions

**5. "Database locked"**

- Close other instances of the app
- Check file permissions on `data/interview_assistant.db`

**6. "File too large"**

- Increase `MAX_FILE_SIZE_MB` in `.env`
- Compress PDF before upload

**7. Audio recording not working in browser**

- Use HTTPS or localhost (browser security)
- Allow microphone permissions
- Try text mode as alternative

### Performance Tips

- Use `WHISPER_MODEL=tiny` for fastest transcription
- Reduce `CHUNK_SIZE` for faster embedding
- Lower `EMBEDDING_BATCH_SIZE` if local document indexing hits low-memory guardrails

## Future Improvements

- [x] Multi-user authentication
- [x] Export reports as PDF
- [ ] Interview scheduling calendar
- [ ] Company-specific question banks
- [ ] Video interview simulation
- [ ] Peer review sharing
- [ ] Mobile-responsive UI
- [x] Offline mode with local LLM (Ollama)
- [ ] Multi-language support
- [ ] Integration with job boards (LinkedIn, Indeed)

## License

MIT License - See LICENSE file for details.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests: `python -m pytest -v`
5. Run lint: `python -m ruff check .`
6. Format code: `python -m black . && python -m ruff check . --fix`
7. Type check: `python -m mypy backend frontend app.py`
8. Submit a pull request

## Final Year Project Presentation Points

### Technical Highlights

1. **Multi-provider LLM Architecture** - Abstracted LLM service supporting Groq, Gemini, and Ollama with fallback
2. **RAG Pipeline** - Complete document processing, embedding, vector storage, and retrieval
3. **Structured Output** - Pydantic models for all LLM responses with validation
4. **Local Speech-to-Text** - Faster-Whisper integration (no paid APIs)
5. **Comprehensive Evaluation** - 5-dimensional answer scoring with actionable feedback
6. **Progress Tracking** - SQLite persistence with historical analytics

### AI/ML Concepts Demonstrated

- Generative AI with LLMs
- Retrieval-Augmented Generation (RAG)
- Vector embeddings and similarity search
- Prompt engineering with structured outputs
- Speech recognition (ASR)
- Multi-modal interaction (text + voice)

### Software Engineering Practices

- Modular, maintainable architecture
- Configuration management with environment variables
- Error handling with retry logic
- Database design with SQLAlchemy ORM
- Unit testing with mocking
- Code formatting (Black, Ruff)
- Git version control

## Viva Questions & Answers

**Q: Why did you choose Groq as the primary LLM provider?**
A: Groq offers low-latency inference and an OpenAI-compatible API, making it easy to swap providers. Quotas and model availability depend on the selected account.

**Q: How does the RAG pipeline work?**
A: Documents → Load → Clean → Split (1000 chars, 200 overlap) → Embed (all-MiniLM-L6-v2) → Store in FAISS → Retrieve top-k → Inject into LLM context.

**Q: Why FAISS over ChromaDB?**
A: FAISS is lighter, faster for CPU, no separate server needed, and sufficient for local document retrieval use case.

**Q: How do you handle LLM rate limits?**
A: Bounded exponential backoff, configurable timeout/retries, rate-limit cooldowns, circuit breakers, and automatic fallback (Groq → Gemini → Ollama).

**Q: How is answer evaluation structured?**
A: 5-dimension rubric (Technical Accuracy, Relevance, Communication, Confidence, Completeness) each 0-10, with qualitative feedback and suggested improvements.

**Q: How do you ensure data privacy?**
A: All processing local except LLM API calls. No data sent to third parties except LLM providers. API keys in `.env` (gitignored). SQLite local database.

**Q: What's the difference between behavioral and situational questions?**
A: Behavioral asks about past experiences (STAR method). Situational presents hypothetical scenarios relevant to the role.

**Q: How does the 7-day improvement plan work?**
A: LLM generates daily actionable items based on interview weaknesses, with specific activities, resources, and time estimates.

## Resume/CV Project Description

**AI Jobs Interview Assistant** | Final Year Project | Python, Streamlit, LLMs, RAG, FAISS, Whisper

- Built a full-stack AI-powered interview preparation platform using Groq, Gemini, and Ollama LLMs, local embeddings (Sentence Transformers), FAISS vector database, and Faster-Whisper for speech-to-text
- Implemented modular RAG pipeline: document loading (PDF/DOCX), text splitting, embedding generation, vector storage, and contextual retrieval
- Designed multi-provider LLM abstraction with automatic fallback, structured output validation (Pydantic), and retry logic with exponential backoff
- Developed 7-category personalized interview question generator with dynamic follow-up based on candidate responses
- Created comprehensive answer evaluation engine scoring 5 dimensions (0-10) with actionable feedback and suggested improvements
- Built interactive Streamlit dashboard with progress tracking, score analytics, and exportable interview reports
- Used SQLite/SQLAlchemy for persistence: user sessions, resume analyses, job matches, interview history, and evaluations
- Applied software engineering best practices: configuration management, error handling, unit testing (pytest), code formatting (Black/Ruff)

## GitHub Project Description

🤖 **AI Jobs Interview Assistant** - An intelligent interview preparation platform that helps students and job seekers practice for real interviews using Generative AI.

**Key Features:**

- 📄 Resume analysis with AI scoring
- 🎯 Job description matching & gap analysis
- 🤖 Personalized interview questions (7 categories, 3 difficulties)
- 🎤 Mock interviews with text/voice input
- 📊 Detailed evaluation & improvement plans
- 📈 Progress tracking dashboard

**Tech Stack:** Python, Streamlit, Groq/Gemini/Ollama LLMs, Sentence Transformers, FAISS, Faster-Whisper, SQLite, Pydantic

**Free & open source:** The code uses free-tier-capable provider options and local models. Provider quotas and model availability depend on the selected account and service.
