# np-website
NetPanzer.io, NetPanzer.com, NetPanzer.com.br

### Dependencies

This is a Django application.

- Python 3.11 (runs on 3.11 in prod)
- DB is bundled - sqlite

### Setup

Create a `.env` file in the root like the following:

```
DEBUG="True"
ENV="dev"
SECRET_KEY="LOCAL_DEV"
```

Install Python, setup environment where dependencies will go, and install them. For example:

```
sudo apt install python3.11 python3.11-venv
python3.11 -m venv env
source env/bin/activate
pip install -r requirements.txt
```

Set up the database:

```
python3.11 manage.py migrate
```

Then run it!

```
python3.11 manage.py runserver
```
