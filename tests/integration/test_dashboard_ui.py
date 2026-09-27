"""The UI must be served without exposing credentials or bypassing API auth."""
from backend.app import create_app
from backend.config import Settings
from fastapi.testclient import TestClient


def test_dashboard_and_modules_are_available_but_api_stays_private(tmp_path):
    secret = 'private-test-token-123456'
    app = create_app(Settings(db_path=str(tmp_path / 'ui.db'), api_token=secret, ndtp_port=0))
    with TestClient(app) as client:
        page = client.get('/')
        assert page.status_code == 200
        assert 'text/html' in page.headers['content-type']
        assert secret not in page.text
        assert client.get('/ui/app.js').status_code == 200
        assert client.get('/ui/styles.css').status_code == 200
        assert client.get('/api/v1/dashboard').status_code == 401
        assert client.get('/ui/../../.env').status_code == 404
