"""Blog Studio: a locally-run app over the translation pipeline.

Stages: idea -> draft (English, AEO-structured) -> translate -> Google Doc.

The pipeline itself is not modified. Everything here is a front-end over
run.process_language, aeo.py, quality.py and publish_gdocs.py.

Start it with:  python -m studio      (or blog.bat)
"""

__all__ = ["ideas", "draft", "english", "jobs", "server"]
