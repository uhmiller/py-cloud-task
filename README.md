<h1 align="center">py-cloud-task</h1>

<p align="center">
    <strong>A Framework Agnostic Client for Google Cloud Tasks.</strong>
    <br>
    Move from "Pull" (Workers) to "Push" (Serverless) architecture effortlessly.
</p>

<p align="center">
    <a href="https://pypi.org/project/py-cloud-task/" target="_blank">
        <img src="https://img.shields.io/pypi/v/py-cloud-task?color=%2334D058&label=pypi%20package" alt="Package version">
    </a>
    <a href="https://pypi.org/project/py-cloud-task/" target="_blank">
        <img src="https://img.shields.io/pypi/pyversions/py-cloud-task.svg?color=%2334D058" alt="Supported Python versions">
    </a>
</p>

---

**py-cloud-task** is a lightweight library that helps you use **Google Cloud Tasks** as your distributed task queue.

It abstracts the complexity of the Google Cloud API and provides a developer experience similar to Celery or TaskIQ, but designed for **Serverless** environments (Cloud Run, App Engine, Cloud Functions).

### Why use this instead of Celery/Redis?

| Feature | Celery / Redis (Pull) | py-cloud-task (Push) |
| :--- | :--- | :--- |
| **Architecture** | Workers poll Redis 24/7 ("Are there tasks?") | Google calls your API via HTTP ("Here is a task") |
| **Cost** | You pay for idle workers & Redis instances | **Pay-per-use** (Scale to Zero supported) |
| **Rate Limiting** | Complex to implement (Redis locks) | **Native** (Google handles throttling) |
| **Retries** | Managed by worker code | **Native** (Exponential backoff managed by GCP) |
| **Maintenance** | High (Monitor Redis, Workers, Memory) | **Zero** (Serverless) |

---

### 📦 Package in development...

<p align="center">
  <a href="https://ziett.com">
    <img src="https://ziett.co/icon.png" alt="Ziett Logo" width="60" height="60"/>
  </a>
  <br>
  <span style="color: #666;">Built with ❤️ by the engineering team at <a href="https://ziett.co">Ziett</a></span>
</p>