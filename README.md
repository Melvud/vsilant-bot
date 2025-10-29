# Tutor's Bot & Admin Panel

[![Build Status](https://img.shields.io/badge/build-passing-brightgreen)](https://github.com/melvud/vsilant-bot)
[![Platform](https://img.shields.io/badge/platform-Telegram%20%7C%20Web-blue)](https://telegram.org/)
[![Python](https://img.shields.io/badge/python-3.12-blueviolet.svg)](https://www.python.org/)
[![Tech](https://img.shields.io/badge/tech-Aiogram%20%7C%20AIOHTTP%20%7C%20PostgreSQL-orange.svg)](https://aiogram.dev/)

A high-performance, asynchronous Telegram bot designed as a **personal assistant for tutors, teachers, and professors**. This application helps manage the educational process by providing two powerful interfaces: a simple `aiogram` bot for quick data entry (like adding grades or tasks) and a secure, comprehensive **Telegram Mini App (TMA)** dashboard for visual management and statistics.

The backend is built entirely in Python using `aiogram` for bot interactions and an `aiohttp` web server to provide a secure REST API for the admin dashboard. All data (students, groups, grades) is stored in a robust PostgreSQL database.



---

## ✨ Core Features

This project provides a complete, dual-interface system for educational management.

### 1. Bot Interface (For the Tutor)

* **Quick Grade Entry** 
* **Guided FSM Flow** 
* **Instant Text Stats**
* **Group Management** 
* **Full Data Export** 

### 2. WebApp Dashboard (Telegram Mini App)

The core of the application: a rich, visual dashboard for the tutor that runs directly inside Telegram.

* **Secure TMA Authentication:** The dashboard is accessible *only* to the admin/tutor. All API requests are authenticated on the backend using Telegram's `initData` hashing verification, ensuring all student data remains private and secure.
* **Visual Statistics:**
    * **Interactive Charts:** Uses **Chart.js** to render beautiful charts for "Performance by Group" and "Daily Activity / Grades".
    * **Stat Cards:** Displays key metrics like "Average Grade (Month)", "Total Submissions (Month)", and "Top Performing Group".
* **Full Grade/Task Management:**
    * **Paginated List:** A "Transactions" (Submissions) tab displays a paginated list of all recent grades and tasks.
    * **Add & Delete:** A clean modal form allows for adding new entries with a date picker, and any entry can be deleted with a single tap.
* **Student Group Management:** A dedicated "Categories" (Groups) tab to visually manage all student groups, add new ones, or delete existing ones.
* **CSV Export:** A one-click button to trigger the `/api/export-csv` endpoint and download the full `.csv` report.

---

## 🛠️ Technology Stack & Architecture

This project is built with a modern, scalable, and fully asynchronous Python stack.

* **Bot Framework:** **Aiogram 3.x**
* **Web Server & API:** **AIOHTTP** & `aiohttp-cors`
* **Database:** **PostgreSQL** (with `asyncpg` driver)
* **Asynchronous:** Fully built on **asyncio** for high throughput.
* **Admin Frontend:** **Telegram Mini App** (HTML, CSS, vanilla JavaScript)
* **Charting:** **Chart.js**
* **State Management:** **Aiogram FSM** (Finite State Machine)
* **Deployment:** Docker, Docker Compose

---

## 👨‍💼 Looking for a Developer?

Hi! I'm the developer behind this project. I specialize in building high-quality, performant, and secure backend systems and bots using Python, `asyncio`, and modern web technologies.

If you're impressed by the clean integration between the `aiogram` bot and the custom-built `aiohttp`-powered Telegram Mini App, I am confident I can bring the same level of expertise and architectural quality to your project.

* **Email:** `ivsilan2005@gmail.com`

Let's build something great together.
