# web-monitoring-processing

---
:information_source: **Welcome Mozilla Global Sprinters!** :wave: :tada: :confetti_ball:  

We're excited to work with you! We've moved our Sprint-specific intro material [into a HackPad](https://hackmd.io/AwFgHMoGwGYLQEYBGUBMcTAIYjlhCuAxkWAKaEhlRZJhA===#) -- please check it out in its new location!


---

A component of the EDGI [Web Monitoring Project](https://github.com/edgi-govdata-archiving/web-monitoring).

## Overview of this component's tasks

This component is intended to hold various backend tools serving different tasks:

1. Query external sources of captured web pages (e.g. Internet Archive, Page
   Freezer, Sentry), and formulate a request for importing their version and
   page metadata into web-monitoring-db.
2. Provide a web service that computes the "diff" between two versions of a page
   in response to a query from web-monitoring-db.
3. Query web-monitoring-db for new Changes, analyze them in an automated
   pipeline to assign priority and/or filter out uninteresting ones, and submit
   this information back to web-monitoring-db.

## Development status

Working and Under Active Development:

* A Python API to PageFreezer's diffing service in
  ``web_monitoring.page_freezer``
* A Python API to the Internet Archive Wayback Machine's archived webpage
  snapshots in ``web_monitoring.internetarchive``
* A Python API to the web-monitoring-db Rails app in ``web_monitoring.db``
* A Python API to PageFreezer's archived snapshots in ``web_monitoring.pf_edgi``
* Python functions and a command-line tool for importing snapshots from PF and
  IA into web-monitoring-db.

Legacy projects that may be revisited:
* [Example HTML](https://github.com/edgi-govdata-archiving/web-monitoring-processing/tree/master/archives) providing useful test cases.
* An experiment with the [newspaper
  module](https://github.com/edgi-govdata-archiving/web-monitoring-processing/tree/master/get_article_text).

## Installation Instructions

1. Get Python 3.6. This packages makes use of modern Python features and
   requires Python 3.6+.  If you don't have Python 3.6, we recommend using
   [conda](https://conda.io/miniconda.html) to install it. (You don't need admin
   privileges to install or use it, and it won't interfere with any other
   installations of Python already on your system.)

2. Install the package.

    ```sh
    python setup.py develop
    ```

3. Copy the script `.env.example` to `.env` and supply any local configuration
   info you need. (Only some of the package's functionality requires this.)
   Apply the configuration:

    ```sh
    source .env
    ```
4. See module comments and docstrings for more usage information. Also see the
   command line tool ``wm``, which is installed with the package. For help, use

   ```sh
   wm --help
   ```

   There is not yet any published documentation.
