# Personal Finance Bot & WebApp

[![Build Status](https://img.shields.io/badge/build-passing-brightgreen)](https://github.com/melvud/vsilant-bot)
[![Platform](https://img.shields.io/badge/platform-Telegram%20%7C%20Web-blue)](https://telegram.org/)
[![Python](https://img.shields.io/badge/python-3.12-blueviolet.svg)](https://www.python.org/)
[![Tech](https://img.shields.io/badge/tech-Aiogram%20%7C%20AIOHTTP%20%7C%20PostgreSQL-orange.svg)](https://aiogram.dev/)

A high-performance, asynchronous Telegram bot for personal expense tracking, fully integrated with a powerful **Telegram Mini App (TMA)** dashboard. This application provides users with two convenient ways to manage their finances: a simple command-based interface via the bot and a rich, visual dashboard via a secure WebApp.

The backend is built entirely in Python using an `aiogram` bot for user interaction and an `aiohttp` web server to provide a secure REST API for the dashboard. All data is stored in a robust PostgreSQL database.

---

## ✨ Core Features

This project is a complete, dual-interface financial tracker.

### 1. Telegram Bot Interface (`client.py`)

* **Quick Add Transactions:** Users can quickly add expenses using a simple command (`/add 100 food`) or a guided **FSM (Finite State Machine)** flow that asks for amount, category, and description.
* **Text-Based Statistics:** A `/stats` command that instantly generates and displays a text report of spending for the week, month, and year.
* **Category Management:** Simple commands (`/add_category`, `/del_category`) to manage custom spending categories.
* **Transaction Deletion:** An FSM-based flow (`/del`) to delete recent transactions.
* **Full Data Export:** A `/export` command that queries the entire transaction history for the user and sends it back as a `.csv` file.

### 2. WebApp Dashboard (`webapp.html` & `api.py`)

This is the core strength of the application—a full-featured dashboard that runs directly inside Telegram.

* **Secure TMA Authentication:** The WebApp is securely loaded *only* for the bot's owner. All API requests are authenticated using Telegram's `initData` hashing mechanism to ensure data privacy and security.
* **Visual Dashboard Tab:**
    * **Charts:** Renders beautiful, interactive charts (using Chart.js) for "Spending by Category" and "Daily Spending".
    * **Stat Cards:** Displays key metrics like "Total Spent (Month)", "Average Daily (Month)", and "Most Expensive Category".
* **Full Transaction Management:**
    * **Paginated List:** A "Transactions" tab that fetches and displays a paginated list of all recent transactions.
    * **Add Transaction Modal:** A clean modal form to add new transactions with date, category, amount, and description.
    * **Delete Transaction:** Users can delete any transaction directly from the list with a single tap.
* **Category Management UI:** A dedicated "Categories" tab to view all existing categories, add new ones, or delete them.
* **CSV Export:** A button to trigger the `/api/export-csv` endpoint and download the full transaction history.

### 3. Backend & Architecture

* **Asynchronous Stack:** Built from the ground up with `asyncio`, using `aiogram` for the bot and `aiohttp` for the web server, enabling high concurrency.
* **Robust Database:** Uses **PostgreSQL** with the `asyncpg` driver for all data storage. The schema (`schema.sql`) is normalized, linking transactions to users and categories.
* **Integrated Entrypoint:** The entire application (bot polling and web server) is launched from a single `backend.py` script.
* **Containerized:** Includes `Dockerfile` and `docker-compose.yml` for easy, reproducible deployment.

---

## 🛠️ Technology Stack & Architecture

This project demonstrates a clean, modern, and scalable Python backend architecture.

* **Bot Framework:** **Aiogram 3.x**
* **Web Server & API:** **AIOHTTP** & `aiohttp-cors`
* **Database:** **PostgreSQL** (with `asyncpg` driver)
* **Frontend (TMA):** **Telegram Mini App** (Vanilla JavaScript, HTML5, CSS3)
* **Charting:** **Chart.js** (loaded via CDN)
* **State Management:** **Aiogram FSM** (Finite State Machine)
* **Deployment:** Docker, Docker Compose

---

## 👨‍💼 Looking for a Developer?

Hi! I'm the developer behind this project. I specialize in building high-quality, performant, and secure backend systems and bots using Python, `asyncio`, and modern web technologies.

If you're impressed by the clean integration between the `aiogram` bot and the custom-built `aiohttp`-powered Telegram Mini App, I am confident I can bring the same level of expertise and architectural quality to your project.

* **Email:** `ivsilan2005@gmail.com`

Let's build something great together.
