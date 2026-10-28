"""READ-ONLY integration smoke: does not place, cancel or arm testnet orders."""
import argparse
import getpass

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    opts = parser.parse_args()
    with httpx.Client(base_url=opts.base_url, timeout=15) as client:
        assert client.get('/healthz').json()['live_trading_enabled'] is False
        client.get('/readyz').raise_for_status()
        auth = client.post('/v1/auth/login', json={
            'username':input('Admin username: '),
            'password':getpass.getpass('Admin password: '),
            'totp':getpass.getpass('MFA code: ')}).json()
        headers = {'Authorization':'Bearer ' + auth['access_token']}
        for path in ('/v1/trading/capabilities','/v1/testnet/capabilities','/v1/testnet/accounts'):
            res = client.get(path, headers=headers)
            res.raise_for_status()
            print(path, res.json())
        caps = client.get('/v1/testnet/capabilities', headers=headers).json()
        assert caps['live_trading_enabled'] is False
        print('PASS: sandbox-only API read routes reachable. No orders placed.')


if __name__ == '__main__':
    main()
