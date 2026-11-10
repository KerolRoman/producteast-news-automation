# Product East - AI News Automation

**Product East AI News Automation** is an ongoing personal software project that streamlines parts of a technology-news editorial workflow. It uses Python to collect news candidates from RSS feeds, filter them by topic, group related headlines into candidate events, and use the OpenAI API with web search to draft an English article for editorial review.

> **Publication safety:** the article generator creates a local HTML article package. It does not publish content to WordPress automatically. Generated articles and source references should be reviewed by a human before publication.

## Features

- **RSS news collection:** collects entries from configured technology, AI, and Asia-focused RSS feeds.
- **Keyword filtering:** matches configured topic keywords with word-boundary-aware matching.
- **Deduplication and persistence:** normalizes URLs, stores stories and feed-run status in SQLite, and tracks publisher metadata.
- **Candidate event clustering:** uses conservative headline similarity heuristics to group likely reports of the same event.
- **AI-assisted editorial selection and drafting:** sends the available clusters to the OpenAI Responses API, with web search enabled, to select a timely topic and return structured article content and SEO metadata.
- **Image retrieval:** searches Wikimedia Commons for illustrative images and downloads suitable results locally; license/attribution metadata is stored alongside the images.
- **HTML article output:** produces a standalone HTML article package with section headings, image placements, alt text, Product East overlay text, source links, and Rank Math-oriented SEO fields.
- **Review-first workflow:** generated output is saved locally for editorial review; automatic publishing is intentionally not part of this workflow.

## Workflow

```text
Configured RSS feeds
        |
        v
Python collector (main.py)
        |
        v
Keyword filtering, URL deduplication, SQLite storage
        |
        v
Candidate export (JSON / CSV / Markdown)
        |
        v
Headline clustering (cluster_stories.py)
        |
        v
OpenAI Responses API + web search (generate_article.py)
        |
        v
Wikimedia Commons image lookup and download
        |
        v
Local HTML article + images + SEO metadata
        |
        v
Human editorial review
```

## Tech stack

- Python
- SQLite
- RSS / feed parsing (`feedparser`)
- HTTP requests (`requests`)
- Date parsing (`python-dateutil`)
- Fuzzy matching (`rapidfuzz`)
- OpenAI Responses API and web search tool
- Wikimedia Commons API
- JSON, CSV, Markdown, HTML
- Windows batch scripts for the local workflow

## Project structure

```text
.
├── main.py                  # RSS collection, filtering, storage, and export
├── cluster_stories.py       # Heuristic headline/event clustering
├── generate_article.py      # AI-assisted article generation and image handling
├── config.json              # RSS feeds, keywords, timezone, and collector settings
├── requirements.txt         # Python dependencies
├── .env.example             # Example API environment configuration
├── run_collector.bat        # Run the collector
├── run_stage2.bat           # Run collection and clustering
├── run_article.bat          # Generate an article from latest clusters
└── tests/
    └── test_clustering.py   # Clustering regression tests
```

Generated data such as the SQLite database, logs, and output files are created locally during execution and should not be committed unless intentionally sanitized for demonstration.

## Setup (Windows)

### 1. Requirements

- Windows
- Python 3.11 or a compatible Python version
- Internet connection
- OpenAI API key for the article-generation stage
- OpenAI API billing enabled for API usage

A ChatGPT subscription does **not** include OpenAI API usage.

### 2. Install dependencies

Open Command Prompt or PowerShell in the project directory:

```powershell
py -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Configure the API key

Copy `.env.example` to `.env` and edit the file:

```dotenv
OPENAI_API_KEY=your_api_key_here
OPENAI_MODEL=gpt-5.4-mini
```

Keep `.env` private. Never commit API keys, passwords, or other secrets. The model setting can be changed to a model available to your API account.

### 4. Run the workflow

Run the Windows batch scripts from the project folder:

- `run_collector.bat` - collects and exports RSS candidate stories.
- `run_stage2.bat` - runs collection and then headline clustering.
- `run_article.bat` - generates an article from the most recent cluster output.

Generated article folders are saved under:

```text
output/articles/<timestamp>_<slug>/
```

Keep the generated HTML file together with its `images` directory when viewing or sharing the article package.

## Tests

Run the clustering regression tests with:

```powershell
python -m unittest discover -s tests -v
```

The included Stage 2 v4 regression report documents the current test run and replay checks. Tests cover software behavior and clustering heuristics; they do not establish the factual accuracy of news articles.

## Configuration

`config.json` controls the configured RSS feeds, topic keywords, timezone, collector limits, lookback window, and output/database paths. Feeds can be enabled or disabled by editing their `enabled` value.

## Limitations and responsible use

- Clustering is heuristic and is **not** fact verification. Similar headlines can refer to different events, and related reports may remain in separate groups.
- AI-generated content can contain errors or unsupported claims. Review article text and linked sources before publication.
- Image retrieval is best-effort. Some queries may produce placeholders or no downloadable image.
- Wikimedia Commons licensing and attribution requirements vary by image. Review the metadata in the generated `image_N_source.json` files before using images publicly.
- RSS feeds and external APIs can change, become unavailable, or impose rate limits.
- This repository describes an evolving personal project; implementation and configuration may change as it is developed.

## Project status

Actively developed. The project is being iterated on to improve source diversity, candidate clustering, article generation, image handling, and the reliability of the editorial workflow.

## Author

Personal project developed for **Product East**, a technology-news website.
