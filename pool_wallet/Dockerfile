FROM python:3.12-slim

WORKDIR /srv/pool_wallet

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests

ENV DATABASE_URL="sqlite:///./pool_wallet.db" \
    POOL_WALLET_API_KEY="change-me" \
    HYPERSWITCH_BASE_URL="http://hyperswitch-server:8080" \
    POOL_WALLET_AUTO_SETTLE="false"

EXPOSE 8090

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
