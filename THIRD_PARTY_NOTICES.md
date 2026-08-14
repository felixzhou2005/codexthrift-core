# Third-Party Notices

CodexThrift Core does not vendor LiteLLM or other third-party dependency source. Python dependencies and exact resolved versions are declared in `pyproject.toml` and `uv.lock`.

The source repository is MIT licensed. A binary Runtime release can redistribute Python packages and native extensions, so its maintainer must complete the following gate before publishing artifacts:

1. Generate the release SBOM and verify it against the resolved Runtime contents.
2. Review the license and attribution terms for every redistributed package, including transitive dependencies.
3. Include any required third-party license texts, notices, source-offer information, and attribution files in the release assets.
4. Record the completed review in the release notes or release manifest without copying credentials or user data.

The current resolved environment includes permissive licenses and at least one MPL-2.0 dependency. That observation is not legal advice and is not a substitute for a version-specific release review.
