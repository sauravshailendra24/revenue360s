from __future__ import annotations
import json
import re
import csv
import io
import time
import zipfile
from decimal import Decimal, InvalidOperation
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse
import requests
from fastapi import APIRouter, Depends, HTTPException
from auth import user_from_header
from db import encrypt_secret, env, execute, insert, json_text, now_iso, one, query, reveal

router = APIRouter(prefix="/api/ads", tags=["ad-platforms"])

PROVIDERS = {
    "meta_ads": {
        "name": "Meta Ads",
        "placements": "Facebook and Instagram",
        "docs": "https://developers.facebook.com/docs/marketing-apis/",
        "credential_urls": [{"label": "Meta app dashboard", "url": "https://developers.facebook.com/apps/"}, {"label": "Graph API Explorer", "url": "https://developers.facebook.com/tools/explorer/"}],
        "setup": "Meta app with Marketing API access, an ad account, and an access token with ads_read or ads_management.",
        "automation": "Build paused campaign, ad set, and ad creative from an existing Page and public image URL; edit ad set budget and targeting; pause or activate campaigns; apply spend guardrails.",
        "reporting": "Last-30-day impressions, clicks, and spend when insights access is granted.",
        "account_platform": "meta_ads",
    },
    "google_ads": {
        "name": "Google Ads",
        "placements": "Search campaigns and YouTube Demand Gen; other account campaign types can be listed where the API supports them",
        "docs": "https://developers.google.com/google-ads/api/docs/get-started/introduction",
        "credential_urls": [{"label": "OAuth credentials", "url": "https://console.cloud.google.com/apis/credentials"}, {"label": "Enable Google Ads API", "url": "https://console.cloud.google.com/apis/library/googleads.googleapis.com"}],
        "setup": "Google Cloud OAuth client, refresh token, and customer ID; API access is attached to the OAuth client’s Cloud project.",
        "automation": "Read campaigns and 30-day performance; create paused Search and YouTube-serving Demand Gen campaigns with assets, groups, and ads; edit dedicated budgets and targeting; pause, activate, or apply spend guardrails.",
        "reporting": "30-day impressions, clicks, cost, and conversions.",
        "account_platform": "google_ads",
    },
    "linkedin_ads": {
        "name": "LinkedIn Ads",
        "placements": "LinkedIn Campaign Manager",
        "docs": "https://learn.microsoft.com/linkedin/marketing/integrations/ads/ads-overview",
        "credential_urls": [{"label": "LinkedIn developer apps", "url": "https://www.linkedin.com/developers/apps"}],
        "setup": "LinkedIn Marketing API access, an approved app with rw_ads and r_ads_reporting, and a sponsored account ID.",
        "automation": "Create paused campaign and text creative; edit campaign budget and targeting; read performance; pause, activate, or apply spend guardrails.",
        "reporting": "30-day impressions, clicks, and spend when r_ads_reporting access is granted.",
        "account_platform": "linkedin_ads",
    },
    "x_ads": {
        "name": "X Ads",
        "placements": "X Ads Manager",
        "docs": "https://developer.x.com/en/docs/x-ads-api",
        "credential_urls": [{"label": "X developer console", "url": "https://console.x.com/"}],
        "setup": "X Ads API access and OAuth user credentials for an ads account.",
        "automation": "Create paused campaigns, line items, and promoted Posts; edit line item budgets and targeting; read performance; pause, activate, or apply spend guardrails.",
        "reporting": "Last-30-day impressions, clicks, and billed spend when X Ads API reporting access is granted.",
        "account_platform": "x_ads",
    },
    "tiktok_ads": {
        "name": "TikTok Ads",
        "placements": "TikTok Ads Manager",
        "docs": "https://business-api.tiktok.com/portal/docs",
        "credential_urls": [{"label": "TikTok for Business apps", "url": "https://business-api.tiktok.com/portal/apps"}],
        "setup": "TikTok for Business developer app, Marketing API access, advertiser ID, and access token.",
        "automation": "Create disabled campaigns, ad groups, and video ads from existing uploaded assets; edit ad group budgets and targeting; read performance; pause, activate, or apply spend guardrails.",
        "reporting": "Last-30-day impressions, clicks, spend, and conversions when Marketing API reporting access is granted.",
        "account_platform": "tiktok_ads",
    },
    "microsoft_ads": {
        "name": "Microsoft Ads",
        "placements": "Bing, Microsoft properties, and partner inventory",
        "docs": "https://learn.microsoft.com/advertising/guides/get-started",
        "credential_urls": [{"label": "Register OAuth app", "url": "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade"}, {"label": "Microsoft Ads developer token", "url": "https://developers.ads.microsoft.com/Account"}],
        "setup": "Microsoft Advertising OAuth access, developer token, customer ID, and account ID.",
        "automation": "Create paused Search campaigns, ad groups, keywords, and responsive search ads; edit budgets and location targeting; read reports; pause, activate, or apply spend guardrails.",
        "reporting": "Last-30-day impressions, clicks, spend, and qualified conversions from Campaign Performance reports.",
        "account_platform": "microsoft_ads",
    },
    "pinterest_ads": {
        "name": "Pinterest Ads",
        "placements": "Pinterest",
        "docs": "https://developers.pinterest.com/docs/work-with-ads/ads-overview/",
        "credential_urls": [{"label": "Pinterest developer apps", "url": "https://developers.pinterest.com/apps/"}],
        "setup": "Pinterest API app with ads access, business account, and ad account ID.",
        "automation": "Create paused campaigns, ad groups, and promoted Pins from existing ad-only Pins; edit budgets and targeting; read performance; pause, activate, or apply spend guardrails.",
        "reporting": "Last-30-day impressions, Pin clicks, spend, and conversions when Ads reporting access is granted.",
        "account_platform": "pinterest_ads",
    },
    "snapchat_ads": {
        "name": "Snapchat Ads",
        "placements": "Snap Ads Manager",
        "docs": "https://developers.snap.com/marketing-api/Ads-API/",
        "credential_urls": [{"label": "Snap Business Manager", "url": "https://business.snapchat.com/"}],
        "setup": "Snap Marketing API app, access token, and ad account ID.",
        "automation": "Create paused campaigns, ad squads, creatives, and ads from uploaded media; edit budgets and targeting; read performance; pause, activate, or apply spend guardrails.",
        "reporting": "Last-30-day impressions, swipes, and spend.",
        "account_platform": "snapchat_ads",
    },
}


def _json(raw: object, default):
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else default
    except (TypeError, json.JSONDecodeError):
        return default


def _account(workspace_id: str, account_id: str) -> tuple[str, dict] | tuple[None, None]:
    account = one(
        "SELECT * FROM ad_network_accounts WHERE id = ? AND workspace_id = ?",
        (account_id, workspace_id),
    )
    if account:
        return "ad_network_accounts", reveal("ad_network_accounts", account) or {}
    account = one(
        "SELECT * FROM meta_ad_accounts WHERE id = ? AND workspace_id = ?",
        (account_id, workspace_id),
    )
    if account:
        return "meta_ad_accounts", reveal("meta_ad_accounts", account) or {}
    return None, None


def _credentials(table: str, account: dict) -> dict:
    if table == "meta_ad_accounts":
        return {"access_token": account.get("access_token") or env("META_ACCESS_TOKEN")}
    credentials = _json(account.get("credentials"), {})
    credentials["__account_id"] = account.get("id")
    credentials["__workspace_id"] = account.get("workspace_id")
    return credentials


def _persist_provider_credentials(credentials: dict) -> None:
    account_id = credentials.get("__account_id")
    workspace_id = credentials.get("__workspace_id")
    if not account_id or not workspace_id:
        return
    safe_credentials = {key: value for key, value in credentials.items() if not key.startswith("__")}
    execute(
        "UPDATE ad_network_accounts SET credentials = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
        (encrypt_secret(json_text(safe_credentials)), now_iso(), account_id, workspace_id),
    )


def _settings(account: dict) -> dict:
    return _json(account.get("settings"), {})


def _fail(response: requests.Response) -> HTTPException:
    try:
        body = response.json()
        message = body.get("error_description") or body.get("message") or body.get("error") or ""
        if isinstance(message, dict):
            message = message.get("message") or message.get("code") or ""
        message = str(message)
    except (ValueError, AttributeError):
        message = ""
    detail = f"Advertising provider returned HTTP {response.status_code}"
    if message:
        detail += f": {message[:220]}"
    return HTTPException(502, detail)


def _request(method: str, url: str, **kwargs) -> requests.Response:
    try:
        response = requests.request(method, url, timeout=25, **kwargs)
    except requests.RequestException as error:
        raise HTTPException(502, f"Advertising provider request failed: {type(error).__name__}") from error
    if not response.ok:
        raise _fail(response)
    return response


def _refresh_google(credentials: dict) -> str:
    access_token = str(credentials.get("access_token") or "")
    refresh_token = str(credentials.get("refresh_token") or "")
    if not refresh_token:
        return access_token
    client_id = credentials.get("client_id") or env("GOOGLE_ADS_CLIENT_ID")
    client_secret = credentials.get("client_secret") or env("GOOGLE_ADS_CLIENT_SECRET")
    if not client_id or not client_secret:
        return access_token
    response = _request(
        "POST",
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    return str(response.json().get("access_token") or access_token)


def _refresh_microsoft(credentials: dict) -> str:
    access_token = str(credentials.get("access_token") or "")
    refresh_token = str(credentials.get("refresh_token") or "")
    client_id = credentials.get("client_id") or env("MICROSOFT_ADS_CLIENT_ID")
    if not refresh_token or not client_id:
        return access_token
    form = {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        "scope": "https://ads.microsoft.com/msads.manage offline_access",
    }
    client_secret = credentials.get("client_secret") or env("MICROSOFT_ADS_CLIENT_SECRET")
    if client_secret:
        form["client_secret"] = client_secret
    response = _request(
        "POST",
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        data=form,
    )
    payload = response.json()
    credentials["access_token"] = str(payload.get("access_token") or access_token)
    if payload.get("refresh_token"):
        credentials["refresh_token"] = str(payload["refresh_token"])
    _persist_provider_credentials(credentials)
    return credentials["access_token"]


def _refresh_snapchat(credentials: dict) -> str:
    access_token = str(credentials.get("access_token") or "")
    refresh_token = str(credentials.get("refresh_token") or "")
    client_id = credentials.get("client_id") or ""
    client_secret = credentials.get("client_secret") or ""
    if not refresh_token or not client_id or not client_secret:
        return access_token
    response = _request("POST", "https://accounts.snapchat.com/login/oauth2/access_token", data={
        "grant_type": "refresh_token", "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token,
    })
    payload = response.json()
    credentials["access_token"] = str(payload.get("access_token") or access_token)
    if payload.get("refresh_token"):
        credentials["refresh_token"] = str(payload["refresh_token"])
    _persist_provider_credentials(credentials)
    return credentials["access_token"]


def _refresh_pinterest(credentials: dict) -> str:
    access_token = str(credentials.get("access_token") or "")
    refresh_token = str(credentials.get("refresh_token") or "")
    client_id = credentials.get("client_id") or ""
    client_secret = credentials.get("client_secret") or ""
    if not refresh_token or not client_id or not client_secret:
        return access_token
    response = _request("POST", "https://api.pinterest.com/v5/oauth/token", auth=(client_id, client_secret), data={
        "grant_type": "refresh_token", "refresh_token": refresh_token, "continuous_refresh": "true",
    })
    payload = response.json()
    credentials["access_token"] = str(payload.get("access_token") or access_token)
    if payload.get("refresh_token"):
        credentials["refresh_token"] = str(payload["refresh_token"])
    _persist_provider_credentials(credentials)
    return credentials["access_token"]


def _meta_campaigns(account: dict, credentials: dict) -> list[dict]:
    token = credentials.get("access_token") or ""
    account_id = account.get("ad_account_id") or env("META_AD_ACCOUNT_ID")
    if not token or not account_id:
        raise HTTPException(400, "Connect a Meta ad account and Marketing API token first.")
    if not account_id.startswith("act_"):
        account_id = f"act_{account_id}"
    version = env("META_GRAPH_API_VERSION", "v25.0")
    response = _request(
        "GET",
        f"https://graph.facebook.com/{version}/{account_id}/campaigns",
        params={
            "access_token": token,
            "fields": "id,name,status,effective_status,objective,daily_budget,lifetime_budget,created_time,insights.date_preset(last_30d){impressions,clicks,spend}",
            "limit": 100,
        },
    )
    campaigns = response.json().get("data") or []
    for campaign in campaigns:
        if campaign.get("daily_budget") not in (None, ""):
            campaign["budget_amount"] = int(campaign["daily_budget"]) / 100
            campaign["currency"] = str(campaign.get("currency") or "")
        rows = (campaign.get("insights") or {}).get("data") or []
        if rows:
            metrics = rows[0]
            campaign["impressions"] = metrics.get("impressions", "")
            campaign["clicks"] = metrics.get("clicks", "")
            try:
                campaign["spend_amount"] = float(metrics.get("spend") or 0)
            except (TypeError, ValueError):
                campaign["spend_amount"] = ""
            campaign["reporting_period"] = "last 30 days"
    return campaigns


def _google_campaigns(account: dict, settings: dict, credentials: dict) -> list[dict]:
    customer_id = str(account.get("advertiser_id") or "").replace("-", "")
    token = _refresh_google(credentials)
    if not customer_id or not token:
        raise HTTPException(400, "Google Ads needs a customer ID and OAuth access or refresh token.")
    version = str(settings.get("api_version") or env("GOOGLE_ADS_API_VERSION", "v25")).strip().strip("/")
    headers = {"Authorization": f"Bearer {token}"}
    manager_id = str(settings.get("login_customer_id") or "").replace("-", "")
    if manager_id:
        headers["login-customer-id"] = manager_id
    campaign_rows = _google_search_rows(
        customer_id, version, headers,
        "SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, "
        "campaign_budget.amount_micros, campaign_budget.resource_name, campaign_budget.explicitly_shared, customer.currency_code "
        "FROM campaign WHERE campaign.status != 'REMOVED' ORDER BY campaign.id DESC LIMIT 100",
    )
    metric_rows = _google_search_rows(
        customer_id, version, headers,
        "SELECT campaign.id, metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions "
        "FROM campaign WHERE campaign.status != 'REMOVED' AND segments.date DURING LAST_30_DAYS",
    )
    metrics_by_campaign = {
        str(row.get("campaign", {}).get("id") or ""): row.get("metrics") or {}
        for row in metric_rows
    }
    campaigns = []
    for row in campaign_rows:
        campaign_id = str(row.get("campaign", {}).get("id") or "")
        metrics = metrics_by_campaign.get(campaign_id, {})
        budget_micros = row.get("campaignBudget", {}).get("amountMicros", "")
        campaigns.append({
            "id": row.get("campaign", {}).get("id", ""),
            "name": row.get("campaign", {}).get("name", ""),
            "status": row.get("campaign", {}).get("status", ""),
            "objective": row.get("campaign", {}).get("advertisingChannelType", ""),
            "budget": budget_micros,
            "budget_amount": int(budget_micros or 0) / 1_000_000,
            "budget_resource": row.get("campaignBudget", {}).get("resourceName", ""),
            "budget_shared": row.get("campaignBudget", {}).get("explicitlyShared", False),
            "currency": row.get("customer", {}).get("currencyCode", ""),
            "impressions": metrics.get("impressions", 0),
            "clicks": metrics.get("clicks", 0),
            "spend_micros": metrics.get("costMicros", 0),
            "spend_amount": int(metrics.get("costMicros", 0) or 0) / 1_000_000,
            "conversions": metrics.get("conversions", 0),
            "conversions_available": "conversions" in metrics,
            "reporting_period": "last 30 days",
        })
    return campaigns


def _google_search_rows(customer_id: str, version: str, headers: dict, gaql: str) -> list[dict]:
    response = _request(
        "POST",
        f"https://googleads.googleapis.com/{version}/customers/{customer_id}/googleAds:searchStream",
        headers=headers,
        json={"query": gaql},
    )
    batches = response.json()
    rows = []
    for batch in batches if isinstance(batches, list) else [batches]:
        rows.extend(batch.get("results") or [])
    return rows


def _microsoft_soap_request(account: dict, credentials: dict, settings: dict, service: str, action: str, request_node: ET.Element) -> bytes:
    namespace = f"https://bingads.microsoft.com/{service}/v13"
    account_id = str(account.get("advertiser_id") or "")
    customer_id = str(settings.get("customer_id") or "")
    token = _refresh_microsoft(credentials)
    developer_token = credentials.get("developer_token") or env("MICROSOFT_ADS_DEVELOPER_TOKEN")
    if not account_id or not customer_id or not token or not developer_token:
        raise HTTPException(400, "Microsoft Ads needs account ID, customer ID, OAuth access token, and developer token.")
    envelope = ET.Element("{http://schemas.xmlsoap.org/soap/envelope/}Envelope")
    header = ET.SubElement(envelope, "{http://schemas.xmlsoap.org/soap/envelope/}Header")
    for name, value in (("Action", action), ("AuthenticationToken", token), ("CustomerAccountId", account_id), ("CustomerId", customer_id), ("DeveloperToken", developer_token)):
        node = ET.SubElement(header, f"{{{namespace}}}{name}")
        if name == "Action":
            node.set("mustUnderstand", "1")
        node.text = value
    body = ET.SubElement(envelope, "{http://schemas.xmlsoap.org/soap/envelope/}Body")
    body.append(request_node)
    endpoint_env = "MICROSOFT_ADS_REPORTING_URL" if service == "Reporting" else "MICROSOFT_ADS_CAMPAIGN_URL"
    default_endpoint = (
        "https://reporting.api.bingads.microsoft.com/Api/Advertiser/Reporting/v13/ReportingService.svc"
        if service == "Reporting" else
        "https://campaign.api.bingads.microsoft.com/Api/Advertiser/CampaignManagement/v13/CampaignManagementService.svc"
    )
    response = _request(
        "POST", env(endpoint_env, default_endpoint),
        data=ET.tostring(envelope, encoding="utf-8", xml_declaration=True),
        headers={"Content-Type": "text/xml; charset=utf-8"},
    )
    return response.content


def _add_microsoft_campaign_metrics(campaigns: list[dict], account: dict, credentials: dict, settings: dict) -> None:
    if not campaigns:
        return
    namespace = "https://bingads.microsoft.com/Reporting/v13"
    ET.register_namespace("r", namespace)
    xsi = "http://www.w3.org/2001/XMLSchema-instance"
    root = ET.Element(f"{{{namespace}}}SubmitGenerateReportRequest")
    report = ET.SubElement(root, f"{{{namespace}}}ReportRequest")
    report.set(f"{{{xsi}}}type", "r:CampaignPerformanceReportRequest")
    for name, value in (("ExcludeColumnHeaders", "false"), ("ExcludeReportFooter", "true"), ("ExcludeReportHeader", "true"), ("Format", "Tsv"), ("ReportName", "Revenue360s campaign performance"), ("ReturnOnlyCompleteData", "false")):
        ET.SubElement(report, f"{{{namespace}}}{name}").text = value
    ET.SubElement(report, f"{{{namespace}}}Aggregation").text = "Summary"
    columns = ET.SubElement(report, f"{{{namespace}}}Columns")
    for column in ("CampaignId", "Impressions", "Clicks", "Spend", "ConversionsQualified"):
        ET.SubElement(columns, f"{{{namespace}}}string").text = column
    scope = ET.SubElement(report, f"{{{namespace}}}Scope")
    ids = ET.SubElement(scope, f"{{{namespace}}}AccountIds")
    ET.SubElement(ids, f"{{{namespace}}}long").text = str(account.get("advertiser_id") or "")
    timing = ET.SubElement(report, f"{{{namespace}}}Time")
    ET.SubElement(timing, f"{{{namespace}}}PredefinedTime").text = "Last30Days"
    try:
        submitted = ET.fromstring(_microsoft_soap_request(account, credentials, settings, "Reporting", "SubmitGenerateReport", root))
        request_id = next((node.text for node in submitted.iter() if node.tag.rsplit("}", 1)[-1] == "ReportRequestId" and node.text), "")
        if not request_id:
            return
        download_url = ""
        for attempt in range(5):
            if attempt:
                time.sleep(2)
            poll = ET.Element(f"{{{namespace}}}PollGenerateReportRequest")
            ET.SubElement(poll, f"{{{namespace}}}ReportRequestId").text = request_id
            response = ET.fromstring(_microsoft_soap_request(account, credentials, settings, "Reporting", "PollGenerateReport", poll))
            status = ""
            for node in response.iter():
                local = node.tag.rsplit("}", 1)[-1]
                if local == "Status":
                    status = node.text or ""
                elif local == "ReportDownloadUrl":
                    download_url = node.text or ""
            if status == "Success" and download_url:
                break
            if status in {"Error", "Expired"}:
                return
        if not download_url:
            return
        archive = _request("GET", download_url, headers={}).content
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            candidates = [name for name in zipped.namelist() if name.lower().endswith((".tsv", ".csv", ".txt"))]
            if not candidates:
                return
            raw = zipped.read(candidates[0]).decode("utf-8-sig", errors="replace")
        rows = list(csv.DictReader(io.StringIO(raw), delimiter="\t"))
        metrics_by_campaign = {}
        for item in rows:
            campaign_id = str(item.get("CampaignId") or item.get("Campaign ID") or "")
            if campaign_id:
                metrics_by_campaign[campaign_id] = item
        for campaign in campaigns:
            metrics = metrics_by_campaign.get(str(campaign.get("id") or ""))
            if not metrics:
                continue
            campaign["impressions"] = metrics.get("Impressions", 0)
            campaign["clicks"] = metrics.get("Clicks", 0)
            campaign["spend_amount"] = float(Decimal(str(metrics.get("Spend") or "0").replace(",", "")))
            campaign["conversions"] = float(Decimal(str(metrics.get("ConversionsQualified") or "0").replace(",", "")))
            campaign["conversions_available"] = True
            campaign["reporting_period"] = "last 30 days"
    except (HTTPException, ValueError, KeyError, zipfile.BadZipFile, InvalidOperation, ET.ParseError, requests.RequestException):
        return


def _linkedin_campaigns(account: dict, settings: dict, credentials: dict) -> list[dict]:
    account_id = str(account.get("advertiser_id") or "")
    token = credentials.get("access_token") or ""
    if not account_id or not token:
        raise HTTPException(400, "LinkedIn Ads needs an ad account ID and Marketing API access token.")
    version = str(settings.get("api_version") or env("LINKEDIN_MARKETING_VERSION", "202608"))
    response = _request(
        "GET",
        f"https://api.linkedin.com/rest/adAccounts/{account_id}/adCampaigns",
        headers={
            "Authorization": f"Bearer {token}",
            "Linkedin-Version": version,
            "X-Restli-Protocol-Version": "2.0.0",
        },
        params={
            "q": "search",
            "search": "(status:(values:List(ACTIVE,PAUSED,ARCHIVED,DRAFT)))",
            "pageSize": 100,
        },
    )
    payload = response.json()
    campaigns = payload.get("elements") or []
    for campaign in campaigns:
        daily_budget = campaign.get("dailyBudget") or {}
        if daily_budget.get("amount") not in (None, ""):
            try:
                campaign["budget_amount"] = float(daily_budget["amount"])
                campaign["currency"] = str(daily_budget.get("currencyCode") or "")
            except (TypeError, ValueError):
                pass
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=29)
    date_range = f"(start:(year:{start.year},month:{start.month},day:{start.day}),end:(year:{today.year},month:{today.month},day:{today.day}))"
    try:
        report = _request(
            "GET", "https://api.linkedin.com/rest/adAnalytics",
            headers={"Authorization": f"Bearer {token}", "Linkedin-Version": version, "X-Restli-Protocol-Version": "2.0.0"},
            params={
                "q": "analytics", "dateRange": date_range, "timeGranularity": "ALL", "pivot": "CAMPAIGN",
                "accounts": f"List(urn:li:sponsoredAccount:{account_id})",
                "fields": "impressions,clicks,costInLocalCurrency,externalWebsiteConversions,pivotValues",
            },
        ).json()
        metrics_by_campaign = {}
        for item in report.get("elements") or []:
            values = item.get("pivotValues") or []
            if not values:
                continue
            campaign_id = str(values[0]).rsplit(":", 1)[-1]
            metrics_by_campaign[campaign_id] = item
        for campaign in campaigns:
            metrics = metrics_by_campaign.get(str(campaign.get("id") or ""))
            if metrics:
                campaign["impressions"] = metrics.get("impressions", 0)
                campaign["clicks"] = metrics.get("clicks", 0)
                campaign["conversions"] = metrics.get("externalWebsiteConversions", 0)
                campaign["conversions_available"] = True
                try:
                    campaign["spend_amount"] = float(metrics.get("costInLocalCurrency") or 0)
                except (TypeError, ValueError):
                    campaign["spend_amount"] = ""
                campaign["reporting_period"] = "last 30 days"
    except HTTPException:
        pass
    return campaigns


def _tiktok_campaigns(account: dict, settings: dict, credentials: dict) -> list[dict]:
    advertiser_id = account.get("advertiser_id") or ""
    token = credentials.get("access_token") or ""
    if not advertiser_id or not token:
        raise HTTPException(400, "TikTok Ads needs an advertiser ID and Marketing API access token.")
    version = str(settings.get("api_version") or "v1.3").strip("/")
    response = _request(
        "GET",
        f"https://business-api.tiktok.com/open_api/{version}/campaign/get/",
        headers={"Access-Token": token},
        params={"advertiser_id": advertiser_id, "page": 1, "page_size": 100},
    )
    payload = response.json()
    if payload.get("code") not in (None, 0):
        raise HTTPException(502, "TikTok Ads rejected the campaign request.")
    campaigns = (payload.get("data") or {}).get("list") or []
    for campaign in campaigns:
        if campaign.get("budget") not in (None, ""):
            try:
                campaign["budget_amount"] = float(campaign["budget"])
            except (TypeError, ValueError):
                pass
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=29)
    try:
        report = _request(
            "GET", f"https://business-api.tiktok.com/open_api/{version}/report/integrated/get/",
            headers={"Access-Token": token},
            params={
                "advertiser_id": advertiser_id, "report_type": "BASIC", "data_level": "AUCTION_CAMPAIGN",
                "dimensions": json.dumps(["campaign_id"]), "metrics": json.dumps(["impressions", "clicks", "spend", "conversion"]),
                "start_date": start.isoformat(), "end_date": today.isoformat(), "page": 1, "page_size": 100,
            },
        ).json()
        report_data = report.get("data") or {}
        if report.get("code") in (None, 0):
            for item in report_data.get("list") or []:
                dimensions = item.get("dimensions") or {}
                metrics = item.get("metrics") or {}
                campaign = next((row for row in campaigns if str(row.get("campaign_id") or row.get("id") or "") == str(dimensions.get("campaign_id") or "")), None)
                if campaign:
                    campaign["impressions"] = metrics.get("impressions", 0)
                    campaign["clicks"] = metrics.get("clicks", 0)
                    if metrics.get("conversion") not in (None, ""):
                        campaign["conversions"] = metrics.get("conversion", 0)
                        campaign["conversions_available"] = True
                    try:
                        campaign["spend_amount"] = float(metrics.get("spend") or 0)
                    except (TypeError, ValueError):
                        campaign["spend_amount"] = ""
                    campaign["reporting_period"] = "last 30 days"
    except HTTPException:
        pass
    return campaigns


def _x_campaigns(account: dict, settings: dict, credentials: dict) -> list[dict]:
    required = ("consumer_key", "consumer_secret", "access_token", "access_token_secret")
    if not account.get("advertiser_id") or any(not credentials.get(key) for key in required):
        raise HTTPException(400, "X Ads needs an account ID and all four OAuth 1.0a credentials.")
    try:
        from requests_oauthlib import OAuth1
    except ImportError as error:
        raise HTTPException(503, "Install requests-oauthlib to enable the X Ads API connector.") from error
    version = str(settings.get("api_version") or "12").strip("/")
    response = _request(
        "GET",
        f"https://ads-api.x.com/{version}/accounts/{quote(str(account['advertiser_id']), safe='')}/campaigns",
        auth=OAuth1(
            credentials["consumer_key"], credentials["consumer_secret"],
            credentials["access_token"], credentials["access_token_secret"],
        ),
        params={"count": 100},
    )
    payload = response.json()
    rows = payload.get("data") or []
    for row in rows:
        if "paused" in row:
            row["status"] = "PAUSED" if row.get("paused") else "ACTIVE"
    campaign_ids = [str(row.get("id") or "") for row in rows if row.get("id")]
    if campaign_ids:
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=29)
        try:
            for offset in range(0, len(campaign_ids), 20):
                report = _request(
                    "GET",
                    f"https://ads-api.x.com/{version}/stats/accounts/{quote(str(account['advertiser_id']), safe='')}",
                    auth=OAuth1(
                        credentials["consumer_key"], credentials["consumer_secret"],
                        credentials["access_token"], credentials["access_token_secret"],
                    ),
                    params={
                        "entity": "CAMPAIGN", "entity_ids": ",".join(campaign_ids[offset:offset + 20]),
                        "start_time": f"{start.isoformat()}T00:00:00Z",
                        "end_time": f"{today.isoformat()}T23:59:59Z",
                        "granularity": "TOTAL", "metric_groups": "ENGAGEMENT,BILLING",
                        "placement": "ALL_ON_TWITTER",
                    },
                ).json()
                for item in report.get("data") or []:
                    campaign = next((row for row in rows if str(row.get("id")) == str(item.get("id"))), None)
                    details = item.get("id_data") or []
                    metrics = (details[0].get("metrics") or {}) if details else (item.get("metrics") or {})
                    if not campaign:
                        continue
                    campaign["impressions"] = _first_metric(metrics, "impressions")
                    campaign["clicks"] = _first_metric(metrics, "url_clicks", "clicks")
                    billed = _first_metric(metrics, "billed_charge_local_micro", "billed_charge_micro")
                    if billed is not None:
                        campaign["spend_amount"] = float(billed) / 1_000_000
                    campaign["reporting_period"] = "last 30 days"
        except (HTTPException, TypeError, ValueError):
            # X returns campaign metadata even when the separate stats product is
            # unavailable for this developer account or an account has no delivery.
            pass
    return rows


def _first_metric(metrics: dict, *names: str):
    for name in names:
        value = metrics.get(name)
        if isinstance(value, list):
            value = value[0] if value else None
        if value not in (None, ""):
            return value
    return None


def _microsoft_campaigns(account: dict, credentials: dict, settings: dict, include_report: bool = True) -> list[dict]:
    account_id = str(account.get("advertiser_id") or "")
    customer_id = str(settings.get("customer_id") or "")
    token = _refresh_microsoft(credentials)
    developer_token = credentials.get("developer_token") or env("MICROSOFT_ADS_DEVELOPER_TOKEN")
    if not account_id or not customer_id or not token or not developer_token:
        raise HTTPException(400, "Microsoft Ads needs account ID, customer ID, OAuth access token, and developer token.")
    ns = "https://bingads.microsoft.com/CampaignManagement/v13"
    envelope = ET.Element("{http://schemas.xmlsoap.org/soap/envelope/}Envelope")
    header = ET.SubElement(envelope, "{http://schemas.xmlsoap.org/soap/envelope/}Header")
    for name, value in (
        ("Action", "GetCampaignsByAccountId"),
        ("AuthenticationToken", token),
        ("CustomerAccountId", account_id),
        ("CustomerId", customer_id),
        ("DeveloperToken", developer_token),
    ):
        header_field = ET.SubElement(header, f"{{{ns}}}{name}")
        if name == "Action":
            header_field.set("mustUnderstand", "1")
        header_field.text = value
    body = ET.SubElement(envelope, "{http://schemas.xmlsoap.org/soap/envelope/}Body")
    operation = ET.SubElement(body, f"{{{ns}}}GetCampaignsByAccountIdRequest")
    ET.SubElement(operation, f"{{{ns}}}AccountId").text = account_id
    ET.SubElement(operation, f"{{{ns}}}CampaignType").text = "Search Shopping Audience"
    endpoint = env(
        "MICROSOFT_ADS_CAMPAIGN_URL",
        "https://campaign.api.bingads.microsoft.com/Api/Advertiser/CampaignManagement/v13/CampaignManagementService.svc",
    )
    response = _request(
        "POST",
        endpoint,
        data=ET.tostring(envelope, encoding="utf-8", xml_declaration=True),
        headers={"Content-Type": "text/xml; charset=utf-8"},
    )
    root = ET.fromstring(response.content)
    rows = []
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] != "Campaign":
            continue
        fields = {child.tag.rsplit("}", 1)[-1]: child.text or "" for child in item}
        rows.append({
            "id": fields.get("Id", ""),
            "name": fields.get("Name", ""),
            "status": fields.get("Status", ""),
            "objective": fields.get("CampaignType", ""),
            "budget_amount": fields.get("DailyBudget", ""),
            "currency": settings.get("currency_code", ""),
            "provider_xml": ET.tostring(item, encoding="unicode"),
        })
    if include_report:
        _add_microsoft_campaign_metrics(rows, account, credentials, settings)
    return rows


def _microsoft_update_campaign(account: dict, settings: dict, credentials: dict, provider_campaign: ET.Element) -> None:
    namespace = "https://bingads.microsoft.com/CampaignManagement/v13"
    request_node = ET.Element(f"{{{namespace}}}UpdateCampaignsRequest")
    campaigns_node = ET.SubElement(request_node, f"{{{namespace}}}Campaigns")
    campaigns_node.append(provider_campaign)
    _microsoft_soap_request(account, credentials, settings, "CampaignManagement", "UpdateCampaigns", request_node)


def _pinterest_campaigns(account: dict, settings: dict, credentials: dict) -> list[dict]:
    account_id = account.get("advertiser_id") or ""
    token = _refresh_pinterest(credentials)
    if not account_id or not token:
        raise HTTPException(400, "Pinterest Ads needs an ad account ID and API access token.")
    response = _request(
        "GET",
        f"https://api.pinterest.com/{str(settings.get('api_version') or 'v5').strip('/')}/ad_accounts/{quote(str(account_id), safe='')}/campaigns",
        headers={"Authorization": f"Bearer {token}"},
        params={"page_size": 100},
    )
    campaigns = [item.get("data") or item for item in response.json().get("items") or []]
    for campaign in campaigns:
        if campaign.get("daily_spend_cap") not in (None, ""):
            campaign["budget_amount"] = int(campaign["daily_spend_cap"]) / 1_000_000
    if campaigns:
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=29)
        campaign_ids = ",".join(str(row.get("id")) for row in campaigns if row.get("id"))
        if campaign_ids:
            try:
                report = _request(
                    "GET",
                    f"https://api.pinterest.com/{str(settings.get('api_version') or 'v5').strip('/')}/ad_accounts/{quote(str(account_id), safe='')}/campaigns/analytics",
                    headers={"Authorization": f"Bearer {token}"},
                    params={
                        "start_date": start.isoformat(), "end_date": today.isoformat(), "campaign_ids": campaign_ids,
                        "columns": "IMPRESSION,CLICKTHROUGH_1,SPEND_IN_MICRO_DOLLAR,TOTAL_CONVERSIONS", "granularity": "TOTAL",
                        "click_window_days": 30, "engagement_window_days": 30, "view_window_days": 1,
                    },
                ).json()
                report_rows = report if isinstance(report, list) else report.get("items", [])
                for item in report_rows:
                    campaign = next((row for row in campaigns if str(row.get("id")) == str(item.get("CAMPAIGN_ID") or item.get("campaign_id"))), None)
                    if campaign:
                        campaign["impressions"] = item.get("IMPRESSION", 0)
                        campaign["clicks"] = item.get("CLICKTHROUGH_1", 0)
                        if item.get("TOTAL_CONVERSIONS") not in (None, ""):
                            campaign["conversions"] = item.get("TOTAL_CONVERSIONS", 0)
                            campaign["conversions_available"] = True
                        try:
                            campaign["spend_amount"] = int(item.get("SPEND_IN_MICRO_DOLLAR") or 0) / 1_000_000
                        except (TypeError, ValueError):
                            campaign["spend_amount"] = ""
                        campaign["reporting_period"] = "last 30 days"
            except HTTPException:
                pass
    return campaigns


def _snapchat_campaigns(account: dict, credentials: dict) -> list[dict]:
    account_id = account.get("advertiser_id") or ""
    token = _refresh_snapchat(credentials)
    if not account_id or not token:
        raise HTTPException(400, "Snapchat Ads needs an ad account ID and Marketing API access token.")
    response = _request(
        "GET",
        f"https://adsapi.snapchat.com/v1/adaccounts/{quote(str(account_id), safe='')}/campaigns",
        headers={"Authorization": f"Bearer {token}"},
        params={"limit": 100, "sort": "updated_at-desc"},
    )
    campaigns = [row.get("campaign") or row for row in response.json().get("campaigns") or []]
    for campaign in campaigns:
        if campaign.get("daily_budget_micro") not in (None, ""):
            campaign["budget_amount"] = int(campaign["daily_budget_micro"]) / 1_000_000
    if campaigns:
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=29)
        try:
            report = _request(
                "GET", f"https://adsapi.snapchat.com/v1/adaccounts/{quote(str(account_id), safe='')}/stats",
                headers={"Authorization": f"Bearer {token}"},
                params={"granularity": "TOTAL", "breakdown": "campaign", "fields": "impressions,swipes,spend,conversion_purchases,conversion_sign_ups",
                        "start_time": f"{start.isoformat()}T00:00:00.000Z", "end_time": f"{today.isoformat()}T23:59:59.999Z",
                        "swipe_up_attribution_window": "28_DAY", "view_attribution_window": "1_DAY"},
            ).json()
            metrics_by_id = {}
            for entry in report.get("total_stats") or []:
                account_total = entry.get("total_stat") or {}
                campaign_totals = (account_total.get("breakdown_stats") or {}).get("campaign") or []
                for campaign_total in campaign_totals:
                    metrics_by_id[str(campaign_total.get("id") or "")] = campaign_total.get("stats") or {}
            for campaign in campaigns:
                metrics = metrics_by_id.get(str(campaign.get("id") or ""))
                if metrics:
                    campaign["impressions"] = metrics.get("impressions", 0)
                    campaign["clicks"] = metrics.get("swipes", 0)
                    if "conversion_purchases" in metrics or "conversion_sign_ups" in metrics:
                        campaign["conversions"] = float(metrics.get("conversion_purchases") or 0) + float(metrics.get("conversion_sign_ups") or 0)
                        campaign["conversions_available"] = True
                    try:
                        campaign["spend_amount"] = int(metrics.get("spend") or 0) / 1_000_000
                    except (TypeError, ValueError):
                        campaign["spend_amount"] = ""
                    campaign["reporting_period"] = "last 30 days"
        except HTTPException:
            pass
    return campaigns


def _campaign_rows(platform: str, table: str, account: dict) -> list[dict]:
    settings = _settings(account)
    credentials = _credentials(table, account)
    if platform == "meta_ads":
        return _meta_campaigns(account, credentials)
    if platform == "google_ads":
        return _google_campaigns(account, settings, credentials)
    if platform == "linkedin_ads":
        return _linkedin_campaigns(account, settings, credentials)
    if platform == "x_ads":
        return _x_campaigns(account, settings, credentials)
    if platform == "tiktok_ads":
        return _tiktok_campaigns(account, settings, credentials)
    if platform == "microsoft_ads":
        return _microsoft_campaigns(account, credentials, settings)
    if platform == "pinterest_ads":
        return _pinterest_campaigns(account, settings, credentials)
    if platform == "snapchat_ads":
        return _snapchat_campaigns(account, credentials)
    raise HTTPException(400, "This ad platform is not supported.")


def _set_campaign_status(platform: str, table: str, account: dict, campaign_id: str, requested: str) -> dict:
    settings = _settings(account)
    credentials = _credentials(table, account)
    active = requested == "ACTIVE"
    if platform == "meta_ads":
        token = credentials.get("access_token") or ""
        if not token:
            raise HTTPException(400, "Connect a Meta Marketing API token first.")
        version = env("META_GRAPH_API_VERSION", "v25.0")
        response = _request(
            "POST", f"https://graph.facebook.com/{version}/{quote(campaign_id, safe='')}",
            data={"status": "ACTIVE" if active else "PAUSED", "access_token": token},
        )
    elif platform == "google_ads":
        customer_id = str(account.get("advertiser_id") or "").replace("-", "")
        token = _refresh_google(credentials)
        if not customer_id or not token:
            raise HTTPException(400, "Google Ads needs a customer ID and OAuth access or refresh token.")
        version = str(settings.get("api_version") or env("GOOGLE_ADS_API_VERSION", "v25")).strip().strip("/")
        headers = {"Authorization": f"Bearer {token}"}
        manager_id = str(settings.get("login_customer_id") or "").replace("-", "")
        if manager_id:
            headers["login-customer-id"] = manager_id
        response = _request(
            "POST",
            f"https://googleads.googleapis.com/{version}/customers/{customer_id}/campaigns:mutate",
            headers=headers,
            json={"operations": [{
                "update": {
                    "resourceName": f"customers/{customer_id}/campaigns/{campaign_id}",
                    "status": "ENABLED" if active else "PAUSED",
                },
                "updateMask": "status",
            }]},
        )
    elif platform == "linkedin_ads":
        token = credentials.get("access_token") or ""
        account_id = str(account.get("advertiser_id") or "")
        if not token or not account_id:
            raise HTTPException(400, "LinkedIn Ads needs an ad account ID and access token.")
        version = str(settings.get("api_version") or env("LINKEDIN_MARKETING_VERSION", "202608"))
        response = _request(
            "POST",
            f"https://api.linkedin.com/rest/adAccounts/{account_id}/adCampaigns/{quote(campaign_id, safe='')}",
            headers={
                "Authorization": f"Bearer {token}",
                "Linkedin-Version": version,
                "X-Restli-Protocol-Version": "2.0.0",
                "X-RestLi-Method": "PARTIAL_UPDATE",
            },
            json={"patch": {"$set": {"status": "ACTIVE" if active else "PAUSED"}}},
        )
    elif platform == "tiktok_ads":
        advertiser_id = account.get("advertiser_id") or ""
        token = credentials.get("access_token") or ""
        if not advertiser_id or not token:
            raise HTTPException(400, "TikTok Ads needs an advertiser ID and access token.")
        version = str(settings.get("api_version") or "v1.3").strip("/")
        response = _request(
            "POST", f"https://business-api.tiktok.com/open_api/{version}/campaign/status/update/",
            headers={"Access-Token": token},
            json={"advertiser_id": advertiser_id, "campaign_ids": [campaign_id],
                  "operation_status": "ENABLE" if active else "DISABLE"},
        )
        payload = response.json()
        if payload.get("code") not in (None, 0):
            raise HTTPException(502, "TikTok Ads rejected the campaign status update.")
    elif platform == "x_ads":
        required = ("consumer_key", "consumer_secret", "access_token", "access_token_secret")
        if not account.get("advertiser_id") or any(not credentials.get(key) for key in required):
            raise HTTPException(400, "X Ads needs an account ID and all four OAuth 1.0a credentials.")
        try:
            from requests_oauthlib import OAuth1
        except ImportError as error:
            raise HTTPException(503, "Install requests-oauthlib to enable the X Ads API connector.") from error
        version = str(settings.get("api_version") or "12").strip("/")
        response = _request(
            "POST",
            f"https://ads-api.x.com/{version}/accounts/{quote(str(account['advertiser_id']), safe='')}/campaigns/{quote(campaign_id, safe='')}",
            auth=OAuth1(credentials["consumer_key"], credentials["consumer_secret"],
                        credentials["access_token"], credentials["access_token_secret"]),
            data={"paused": "false" if active else "true"},
        )
    elif platform == "pinterest_ads":
        account_id = account.get("advertiser_id") or ""
        token = _refresh_pinterest(credentials)
        if not account_id or not token:
            raise HTTPException(400, "Pinterest Ads needs an ad account ID and access token.")
        response = _request(
            "PATCH", f"https://api.pinterest.com/{str(settings.get('api_version') or 'v5').strip('/')}/ad_accounts/{quote(str(account_id), safe='')}/campaigns",
            headers={"Authorization": f"Bearer {token}"},
            json=[{"id": campaign_id, "status": "ACTIVE" if active else "PAUSED"}],
        )
    elif platform == "snapchat_ads":
        account_id = account.get("advertiser_id") or ""
        token = _refresh_snapchat(credentials)
        if not account_id or not token:
            raise HTTPException(400, "Snapchat Ads needs an ad account ID and access token.")
        response = _request(
            "PATCH",
            f"https://adsapi.snapchat.com/v1/adaccounts/{quote(str(account_id), safe='')}/campaigns/{quote(campaign_id, safe='')}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json-patch+json"},
            json=[{"op": "replace", "path": "/status", "value": "ACTIVE" if active else "PAUSED"}],
        )
    elif platform == "microsoft_ads":
        account_id = str(account.get("advertiser_id") or "")
        customer_id = str(settings.get("customer_id") or "")
        token = _refresh_microsoft(credentials)
        developer_token = credentials.get("developer_token") or env("MICROSOFT_ADS_DEVELOPER_TOKEN")
        if not account_id or not customer_id or not token or not developer_token:
            raise HTTPException(400, "Microsoft Ads needs account ID, customer ID, OAuth access token, and developer token.")
        campaign = next((row for row in _microsoft_campaigns(account, credentials, settings, include_report=False) if str(row.get("id")) == str(campaign_id)), None)
        if not campaign or not campaign.get("provider_xml"):
            raise HTTPException(404, "Microsoft Ads campaign was not found in this account.")
        provider_campaign = ET.fromstring(campaign["provider_xml"])
        status_field = next((child for child in provider_campaign if child.tag.rsplit("}", 1)[-1] == "Status"), None)
        if status_field is None:
            raise HTTPException(502, "Microsoft Ads did not return the campaign status field needed for an update.")
        status_field.text = "Active" if active else "Paused"
        _microsoft_update_campaign(account, settings, credentials, provider_campaign)
        return {"ok": True, "provider": platform}
    else:
        raise HTTPException(400, "This ad platform does not support campaign status changes.")
    if response.content:
        try:
            return {"ok": True, "provider": platform, "campaign": response.json()}
        except ValueError:
            return {"ok": True, "provider": platform}
    return {"ok": True, "provider": platform}


@router.get("/providers")
def list_providers(user: dict = Depends(user_from_header)) -> list[dict]:
    workspace_id = user["workspace_id"]
    rows = query(
        "SELECT id, platform, name, advertiser_id, status, created_at, updated_at FROM ad_network_accounts WHERE workspace_id = ? ORDER BY created_at DESC",
        (workspace_id,),
    )
    legacy_meta = query(
        "SELECT id, name, ad_account_id AS advertiser_id, access_token, status, created_at, updated_at FROM meta_ad_accounts WHERE workspace_id = ? ORDER BY created_at DESC",
        (workspace_id,),
    )
    for row in legacy_meta:
        row["platform"] = "meta_ads"
        if row.get("access_token") or env("META_ACCESS_TOKEN"):
            if str(row.get("status") or "").lower() != "validated":
                row["status"] = "configured"
        elif row.get("status") == "connected":
            row["status"] = "pending"
    accounts_by_platform: dict[str, list[dict]] = {}
    for row in [*rows, *legacy_meta]:
        connection_status = str(row.get("status") or "pending").lower()
        if connection_status == "connected":
            connection_status = "configured"
        accounts_by_platform.setdefault(row["platform"], []).append({
            "id": row["id"],
            "name": row.get("name") or row.get("advertiser_id") or "",
            "identifier": row.get("advertiser_id") or "",
            "status": connection_status,
        })
    result = []
    for provider_id, provider in PROVIDERS.items():
        accounts = accounts_by_platform.get(provider_id, [])
        states = {str(account.get("status") or "pending").lower() for account in accounts}
        state = "validated" if "validated" in states else ("configured" if "configured" in states else ("pending" if accounts else "not_connected"))
        result.append({"id": provider_id, **provider, "accounts": accounts, "status": state})
    return result


@router.get("/platform-accounts/{account_id}/campaigns")
def list_platform_campaigns(account_id: str, user: dict = Depends(user_from_header)) -> dict:
    table, account = _account(user["workspace_id"], account_id)
    if not table or not account:
        raise HTTPException(404, "Ad account not found in this workspace.")
    platform = account.get("platform") or "meta_ads"
    if platform not in PROVIDERS:
        raise HTTPException(400, "This account is not an advertising account.")
    campaigns = _campaign_rows(platform, table, account)
    execute(
        f"UPDATE {table} SET status = 'validated', updated_at = ? WHERE id = ? AND workspace_id = ?",
        (now_iso(), account_id, user["workspace_id"]),
    )
    public_campaigns = [{key: value for key, value in campaign.items() if key != "provider_xml"} for campaign in campaigns]
    return {"platform": platform, "account_id": account_id, "campaigns": public_campaigns, "synced": True}


@router.post("/platform-accounts/{account_id}/campaigns/{campaign_id}/status")
def update_platform_campaign_status(account_id: str, campaign_id: str, payload: dict, user: dict = Depends(user_from_header)) -> dict:
    table, account = _account(user["workspace_id"], account_id)
    if not table or not account:
        raise HTTPException(404, "Ad account not found in this workspace.")
    requested = str(payload.get("status") or "").upper()
    if requested not in {"ACTIVE", "PAUSED"}:
        raise HTTPException(400, "Status must be ACTIVE or PAUSED.")
    platform = account.get("platform") or "meta_ads"
    return _set_campaign_status(platform, table, account, campaign_id, requested)


def _google_headers(account: dict, settings: dict, credentials: dict) -> tuple[str, str, str, dict]:
    customer_id = str(account.get("advertiser_id") or "").replace("-", "")
    token = _refresh_google(credentials)
    if not customer_id.isdigit() or not token:
        raise HTTPException(400, "Google Ads needs a numeric customer ID and OAuth access or refresh token.")
    version = str(settings.get("api_version") or env("GOOGLE_ADS_API_VERSION", "v25")).strip().strip("/")
    headers = {"Authorization": f"Bearer {token}"}
    manager_id = str(settings.get("login_customer_id") or "").replace("-", "")
    if manager_id:
        headers["login-customer-id"] = manager_id
    return customer_id, token, version, headers


def _created_id(payload: dict, *paths: str) -> str:
    for path in paths:
        value = payload
        for part in path.split("."):
            if isinstance(value, dict):
                value = value.get(part)
            elif isinstance(value, list) and part.isdigit():
                index = int(part)
                value = value[index] if index < len(value) else None
            else:
                value = None
        if value not in (None, ""):
            return str(value)
    return ""


def _ad_decimal(value: object, label: str = "budget") -> Decimal:
    try:
        number = Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError):
        raise HTTPException(400, f"Enter a valid {label} in the ad account currency.")
    if not number.is_finite() or number <= 0:
        raise HTTPException(400, f"{label.capitalize()} must be greater than zero.")
    return number


def _create_meta_campaign(account: dict, credentials: dict, payload: dict) -> dict:
    token = credentials.get("access_token") or ""
    ad_account_id = str(account.get("ad_account_id") or account.get("advertiser_id") or env("META_AD_ACCOUNT_ID"))
    if not token or not ad_account_id:
        raise HTTPException(400, "Meta Ads needs an ad account ID and a Marketing API access token.")
    if not ad_account_id.startswith("act_"):
        ad_account_id = f"act_{ad_account_id}"
    name = str(payload.get("name") or "").strip()
    page_id = str(payload.get("page_id") or "").strip()
    final_url = str(payload.get("final_url") or "").strip()
    image_url = str(payload.get("image_url") or "").strip()
    country = str(payload.get("country_code") or "US").strip().upper()
    parsed_url = urlparse(final_url)
    if not page_id or parsed_url.scheme != "https" or not parsed_url.netloc or not image_url.startswith("https://"):
        raise HTTPException(400, "Meta Ads needs a Page ID, public HTTPS landing page, and public HTTPS image URL.")
    objective = str(payload.get("objective") or "OUTCOME_TRAFFIC").upper()
    if objective != "OUTCOME_TRAFFIC":
        raise HTTPException(400, "This Meta builder currently supports OUTCOME_TRAFFIC, paired with link-click optimization.")
    version = env("META_GRAPH_API_VERSION", "v25.0")
    base = f"https://graph.facebook.com/{version}"
    budget_cents = int(_ad_decimal(payload.get("daily_budget")) * 100)
    campaign = _request("POST", f"{base}/{ad_account_id}/campaigns", data={
        "name": name, "objective": objective, "status": "PAUSED",
        "special_ad_categories": "[]", "is_adset_budget_sharing_enabled": "false", "access_token": token,
    }).json()
    campaign_id = str(campaign.get("id") or "")
    if not campaign_id:
        raise HTTPException(502, "Meta created no campaign ID; check the Marketing API response.")
    try:
        adset = _request("POST", f"{base}/{ad_account_id}/adsets", data={
            "name": f"{name} - ad set", "campaign_id": campaign_id, "status": "PAUSED",
            "daily_budget": str(budget_cents), "billing_event": "IMPRESSIONS",
            "optimization_goal": "LINK_CLICKS", "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            "targeting": json.dumps({"geo_locations": {"countries": [country]}}), "access_token": token,
        }).json()
        adset_id = str(adset.get("id") or "")
        if not adset_id:
            raise HTTPException(502, "Meta did not return an ad set ID.")
        creative = _request("POST", f"{base}/{ad_account_id}/adcreatives", data={
            "name": f"{name} - creative", "object_story_spec": json.dumps({
                "page_id": page_id,
                "link_data": {
                    "link": final_url, "picture": image_url,
                    "message": str(payload.get("body") or "").strip(),
                    "name": str(payload.get("headline") or name).strip(),
                    "call_to_action": {"type": "LEARN_MORE", "value": {"link": final_url}},
                },
            }), "access_token": token,
        }).json()
        creative_id = str(creative.get("id") or "")
        if not creative_id:
            raise HTTPException(502, "Meta did not return an ad creative ID.")
        ad = _request("POST", f"{base}/{ad_account_id}/ads", data={
            "name": f"{name} - ad", "adset_id": adset_id, "status": "PAUSED",
            "creative": json.dumps({"creative_id": creative_id}), "access_token": token,
        }).json()
        ad_id = str(ad.get("id") or "")
        if not ad_id:
            raise HTTPException(502, "Meta did not return an ad ID.")
    except HTTPException as error:
        raise HTTPException(502, f"Meta campaign {campaign_id} exists but remains paused. Ad setup failed: {error.detail}") from error
    return {"ok": True, "provider": "meta_ads", "campaign_id": campaign_id, "ad_group_id": adset_id, "creative_id": creative_id, "ad_id": ad_id, "status": "PAUSED"}


def _linkedin_headers(settings: dict, credentials: dict) -> dict:
    token = credentials.get("access_token") or ""
    if not token:
        raise HTTPException(400, "LinkedIn Ads needs a Marketing API access token.")
    return {"Authorization": f"Bearer {token}", "Linkedin-Version": str(settings.get("api_version") or env("LINKEDIN_MARKETING_VERSION", "202608")), "X-Restli-Protocol-Version": "2.0.0"}


def _create_linkedin_campaign(account: dict, settings: dict, credentials: dict, payload: dict) -> dict:
    account_id = str(account.get("advertiser_id") or "")
    campaign_group = str(payload.get("campaign_group_urn") or settings.get("campaign_group_urn") or "")
    final_url = str(payload.get("final_url") or "").strip()
    name = str(payload.get("name") or "").strip()
    currency = str(payload.get("currency_code") or settings.get("currency_code") or "USD").upper()
    country = str(payload.get("country_code") or "US").upper()
    language = str(payload.get("language_code") or "en").lower()
    if not account_id or not campaign_group or not urlparse(final_url).scheme == "https":
        raise HTTPException(400, "LinkedIn Ads needs a campaign group URN and public HTTPS landing page. Set the group URN in account settings or this builder.")
    headers = _linkedin_headers(settings, credentials)
    amount = _ad_decimal(payload.get("daily_budget"))
    targeting = payload.get("targeting_criteria") if isinstance(payload.get("targeting_criteria"), dict) else {}
    if str(payload.get("objective") or "WEBSITE_VISITS").upper() != "WEBSITE_VISITS":
        raise HTTPException(400, "This LinkedIn text-ad builder currently supports the WEBSITE_VISITS objective.")
    if not targeting:
        raise HTTPException(400, "LinkedIn campaigns need non-empty targeting_criteria JSON with an include audience.")
    campaign_body = {
        "account": f"urn:li:sponsoredAccount:{account_id.removeprefix('urn:li:sponsoredAccount:')}",
        "campaignGroup": campaign_group, "name": name, "status": "PAUSED",
        "type": "TEXT_AD",
        "objectiveType": str(payload.get("objective") or "WEBSITE_VISITS").upper(),
        "costType": "CPC", "dailyBudget": {"amount": str(amount), "currencyCode": currency},
        "locale": {"country": country, "language": language}, "targetingCriteria": targeting,
    }
    created = _request("POST", f"https://api.linkedin.com/rest/adAccounts/{quote(account_id, safe='')}/adCampaigns", headers=headers, json=campaign_body)
    campaign_id = str(created.headers.get("x-restli-id") or _created_id(created.json(), "id"))
    if not campaign_id:
        raise HTTPException(502, "LinkedIn did not return a campaign ID.")
    creative_body = {
        "campaign": f"urn:li:sponsoredCampaign:{campaign_id}", "type": "TEXT_AD", "status": "PAUSED",
        "variables": {"com.linkedin.ads.TextAdCreativeVariables": {
            "clickUri": final_url,
            "data": {"com.linkedin.ads.TextAdCreativeVariables": {
                "title": str(payload.get("headline") or name)[:25], "text": str(payload.get("body") or name)[:75],
            }},
        }},
    }
    try:
        creative_response = _request("POST", f"https://api.linkedin.com/rest/adAccounts/{quote(account_id, safe='')}/creatives", headers=headers, json=creative_body)
        creative_id = str(creative_response.headers.get("x-restli-id") or _created_id(creative_response.json(), "id"))
    except HTTPException as error:
        raise HTTPException(502, f"LinkedIn campaign {campaign_id} exists but remains paused. Creative setup failed: {error.detail}") from error
    return {"ok": True, "provider": "linkedin_ads", "campaign_id": campaign_id, "creative_id": creative_id, "status": "PAUSED"}


def _tiktok_response_data(response: requests.Response) -> dict:
    payload = response.json()
    if payload.get("code") not in (None, 0):
        raise HTTPException(502, f"TikTok Ads rejected the request: {str(payload.get('message') or payload.get('msg') or 'provider error')[:180]}")
    return payload.get("data") or {}


def _create_tiktok_campaign(account: dict, settings: dict, credentials: dict, payload: dict) -> dict:
    advertiser_id = str(account.get("advertiser_id") or "")
    token = credentials.get("access_token") or ""
    name = str(payload.get("name") or "").strip()
    video_id = str(payload.get("video_id") or "").strip()
    identity_id = str(payload.get("identity_id") or "").strip()
    location_id = str(payload.get("location_id") or "").strip()
    final_url = str(payload.get("final_url") or "").strip()
    if not advertiser_id or not token or not video_id or not identity_id or not location_id or not urlparse(final_url).scheme == "https":
        raise HTTPException(400, "TikTok Ads needs an uploaded video ID, authorized identity ID, location ID, and public HTTPS landing page.")
    version = str(settings.get("api_version") or "v1.3").strip("/")
    base = f"https://business-api.tiktok.com/open_api/{version}"
    budget = float(_ad_decimal(payload.get("daily_budget")))
    objective = str(payload.get("objective") or "TRAFFIC").upper()
    if objective != "TRAFFIC":
        raise HTTPException(400, "This TikTok builder currently supports the TRAFFIC objective with click optimization.")
    campaign = _tiktok_response_data(_request("POST", f"{base}/campaign/create/", headers={"Access-Token": token}, json={
        "advertiser_id": advertiser_id, "campaign_name": name, "objective_type": objective,
        "budget_mode": "BUDGET_MODE_DAY", "budget": budget, "operation_status": "DISABLE",
    }))
    campaign_id = str(campaign.get("campaign_id") or "")
    if not campaign_id:
        raise HTTPException(502, "TikTok did not return a campaign ID.")
    local_start = datetime.now().astimezone().replace(second=0, microsecond=0) + timedelta(minutes=10)
    adgroup = _tiktok_response_data(_request("POST", f"{base}/adgroup/create/", headers={"Access-Token": token}, json={
        "advertiser_id": advertiser_id, "campaign_id": campaign_id,
        "adgroup_name": f"{name} - ad group", "placement_type": "PLACEMENT_TYPE_NORMAL", "placements": ["PLACEMENT_TIKTOK"],
        "location_ids": [location_id], "promotion_type": "WEBSITE", "promotion_website_type": "EXTERNAL_WEBSITE",
        "budget_mode": "BUDGET_MODE_DAY", "budget": budget, "billing_event": "CPC",
        "optimization_goal": "CLICK",
        "schedule_type": "SCHEDULE_FROM_NOW", "schedule_start_time": local_start.strftime("%Y-%m-%d %H:%M:%S"),
        "pacing": "PACING_MODE_SMOOTH", "operation_status": "DISABLE",
    }))
    adgroup_id = str(adgroup.get("adgroup_id") or "")
    if not adgroup_id:
        raise HTTPException(502, f"TikTok campaign {campaign_id} exists disabled, but no ad group ID was returned.")
    creative = {
        "ad_name": f"{name} - ad", "ad_format": "SINGLE_VIDEO", "ad_text": str(payload.get("body") or name)[:100],
        "video_id": video_id, "identity_id": identity_id,
        "identity_type": str(payload.get("identity_type") or "BC_AUTH_TT").upper(),
        "call_to_action": str(payload.get("call_to_action") or "LEARN_MORE").upper(), "landing_page_url": final_url,
    }
    try:
        ad_data = _tiktok_response_data(_request("POST", f"{base}/ad/create/", headers={"Access-Token": token}, json={
            "advertiser_id": advertiser_id, "adgroup_id": adgroup_id, "creatives": [creative],
        }))
    except HTTPException as error:
        raise HTTPException(502, f"TikTok campaign {campaign_id} and ad group {adgroup_id} remain disabled. Ad setup failed: {error.detail}") from error
    ad_id = _created_id(ad_data, "ad_ids.0", "ad_id")
    return {"ok": True, "provider": "tiktok_ads", "campaign_id": campaign_id, "ad_group_id": adgroup_id, "ad_id": ad_id, "status": "DISABLED"}


def _create_pinterest_campaign(account: dict, settings: dict, credentials: dict, payload: dict) -> dict:
    account_id = str(account.get("advertiser_id") or "")
    token = _refresh_pinterest(credentials)
    pin_id = str(payload.get("pin_id") or "").strip()
    country = str(payload.get("country_code") or "US").upper()
    if str(payload.get("objective") or "CONSIDERATION").upper() != "CONSIDERATION":
        raise HTTPException(400, "This Pinterest builder currently supports the CONSIDERATION objective.")
    if not account_id or not token or not pin_id:
        raise HTTPException(400, "Pinterest Ads needs an ad account, access token, and existing ad-only Pin ID.")
    version = str(settings.get("api_version") or "v5").strip("/")
    base = f"https://api.pinterest.com/{version}/ad_accounts/{quote(account_id, safe='')}"
    headers = {"Authorization": f"Bearer {token}"}
    budget_micros = int(_ad_decimal(payload.get("daily_budget")) * 1_000_000)
    campaign_result = _request("POST", f"{base}/campaigns", headers=headers, json=[{
        "name": str(payload.get("name") or "").strip(), "status": "PAUSED",
        "objective_type": str(payload.get("objective") or "CONSIDERATION").upper(),
        "daily_spend_cap": budget_micros, "is_flexible_daily_budgets": False,
        "is_campaign_budget_optimization": True,
    }]).json()
    campaign_id = _created_id(campaign_result, "items.0.data.id", "items.0.id", "campaigns.0.id")
    if not campaign_id:
        raise HTTPException(502, "Pinterest did not return a campaign ID.")
    group_result = _request("POST", f"{base}/ad_groups", headers=headers, json=[{
        "name": f"{str(payload.get('name') or '').strip()} - ad group", "campaign_id": campaign_id,
        "status": "PAUSED", "billable_event": "CLICKTHROUGH", "targeting_spec": {"GEO": [country]},
        "bid_in_micro_currency": int(_ad_decimal(payload.get("bid") or "1") * 1_000_000),
    }]).json()
    ad_group_id = _created_id(group_result, "items.0.data.id", "items.0.id", "ad_groups.0.id")
    if not ad_group_id:
        raise HTTPException(502, f"Pinterest campaign {campaign_id} exists paused, but no ad group ID was returned.")
    try:
        ad_result = _request("POST", f"{base}/ads", headers=headers, json=[{
            "ad_group_id": ad_group_id, "creative_type": str(payload.get("creative_type") or "REGULAR").upper(),
            "pin_id": pin_id, "is_removable": True, "status": "PAUSED",
        }]).json()
        ad_id = _created_id(ad_result, "items.0.data.id", "items.0.id", "ads.0.id")
    except HTTPException as error:
        raise HTTPException(502, f"Pinterest campaign {campaign_id} and ad group {ad_group_id} remain paused. Ad setup failed: {error.detail}") from error
    return {"ok": True, "provider": "pinterest_ads", "campaign_id": campaign_id, "ad_group_id": ad_group_id, "ad_id": ad_id, "status": "PAUSED"}


def _create_snapchat_campaign(account: dict, credentials: dict, payload: dict) -> dict:
    account_id = str(account.get("advertiser_id") or "")
    token = _refresh_snapchat(credentials)
    media_id = str(payload.get("media_id") or "").strip()
    profile_id = str(payload.get("profile_id") or "").strip()
    country = str(payload.get("country_code") or "us").lower()
    if str(payload.get("objective") or "AWARENESS_AND_ENGAGEMENT").upper() != "AWARENESS_AND_ENGAGEMENT":
        raise HTTPException(400, "This Snap builder currently supports the AWARENESS_AND_ENGAGEMENT objective.")
    if not account_id or not token or not media_id or not profile_id:
        raise HTTPException(400, "Snap Ads needs an ad account, access token, uploaded Top Snap media ID, and Public Profile ID.")
    base = "https://adsapi.snapchat.com/v1"
    headers = {"Authorization": f"Bearer {token}"}
    budget_micro = int(_ad_decimal(payload.get("daily_budget")) * 1_000_000)
    campaign_payload = _request("POST", f"{base}/adaccounts/{quote(account_id, safe='')}/campaigns", headers=headers, json={"campaigns": [{
        "name": str(payload.get("name") or "").strip(), "ad_account_id": account_id, "status": "PAUSED",
        "start_time": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "daily_budget_micro": budget_micro, "buy_model": "AUCTION",
        "objective_v2_properties": {"objective_v2_type": str(payload.get("objective") or "AWARENESS_AND_ENGAGEMENT").upper()},
    }]}).json()
    campaign_id = _created_id(campaign_payload, "campaigns.0.campaign.id", "campaigns.0.id", "campaign.id")
    if not campaign_id:
        raise HTTPException(502, "Snap did not return a campaign ID.")
    squad_payload = _request("POST", f"{base}/campaigns/{quote(campaign_id, safe='')}/adsquads", headers=headers, json={"adsquads": [{
        "campaign_id": campaign_id, "name": f"{str(payload.get('name') or '').strip()} - ad squad", "type": "SNAP_ADS", "status": "PAUSED",
        "placement_v2": {"config": "AUTOMATIC"}, "optimization_goal": "IMPRESSIONS", "bid_micro": int(_ad_decimal(payload.get("bid") or "1") * 1_000_000),
        "daily_budget_micro": budget_micro, "bid_strategy": "LOWEST_COST_WITH_MAX_BID", "billing_event": "IMPRESSION",
        "targeting": {"geos": [{"country_code": country}]},
        "start_time": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }]}).json()
    squad_id = _created_id(squad_payload, "adsquads.0.adsquad.id", "adsquads.0.id", "ad_squads.0.id")
    if not squad_id:
        raise HTTPException(502, f"Snap campaign {campaign_id} exists paused, but no ad squad ID was returned.")
    creative_payload = _request("POST", f"{base}/adaccounts/{quote(account_id, safe='')}/creatives", headers=headers, json={"creatives": [{
        "ad_account_id": account_id, "name": f"{str(payload.get('name') or '').strip()} - creative", "type": "SNAP_AD",
        "top_snap_media_id": media_id, "headline": str(payload.get("headline") or "").strip(),
        "profile_properties": {"profile_id": profile_id},
    }]}).json()
    creative_id = _created_id(creative_payload, "creatives.0.creative.id", "creatives.0.id", "creative.id")
    if not creative_id:
        raise HTTPException(502, f"Snap campaign {campaign_id} and ad squad {squad_id} remain paused, but no creative ID was returned.")
    try:
        ad_payload = _request("POST", f"{base}/adsquads/{quote(squad_id, safe='')}/ads", headers=headers, json={"ads": [{
            "ad_squad_id": squad_id, "creative_id": creative_id, "name": f"{str(payload.get('name') or '').strip()} - ad", "type": "SNAP_AD", "status": "PAUSED",
        }]}).json()
        ad_id = _created_id(ad_payload, "ads.0.ad.id", "ads.0.id", "ad.id")
    except HTTPException as error:
        raise HTTPException(502, f"Snap campaign {campaign_id} and ad squad {squad_id} remain paused. Ad setup failed: {error.detail}") from error
    return {"ok": True, "provider": "snapchat_ads", "campaign_id": campaign_id, "ad_group_id": squad_id, "creative_id": creative_id, "ad_id": ad_id, "status": "PAUSED"}


def _x_oauth(credentials: dict):
    required = ("consumer_key", "consumer_secret", "access_token", "access_token_secret")
    if any(not credentials.get(key) for key in required):
        raise HTTPException(400, "X Ads needs OAuth consumer key/secret and user access token/secret.")
    try:
        from requests_oauthlib import OAuth1
    except ImportError as error:
        raise HTTPException(503, "Install requests-oauthlib to enable the X Ads API connector.") from error
    return OAuth1(credentials["consumer_key"], credentials["consumer_secret"], credentials["access_token"], credentials["access_token_secret"])


def _create_x_campaign(account: dict, settings: dict, credentials: dict, payload: dict) -> dict:
    account_id = str(account.get("advertiser_id") or "")
    funding_id = str(payload.get("funding_instrument_id") or settings.get("funding_instrument_id") or "")
    tweet_id = str(payload.get("tweet_id") or "")
    if not account_id or not funding_id or not tweet_id:
        raise HTTPException(400, "X Ads needs an account, funding instrument ID, and an existing approved Post ID to promote.")
    auth = _x_oauth(credentials)
    version = str(settings.get("api_version") or "12").strip("/")
    base = f"https://ads-api.x.com/{version}/accounts/{quote(account_id, safe='')}"
    budget_micro = int(_ad_decimal(payload.get("daily_budget")) * 1_000_000)
    created = _request("POST", f"{base}/campaigns", auth=auth, json={
        "name": str(payload.get("name") or "").strip(), "funding_instrument_id": funding_id,
        "start_time": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "daily_budget_amount_local_micro": budget_micro, "paused": True,
    }).json()
    campaign_id = _created_id(created, "data.id")
    if not campaign_id:
        raise HTTPException(502, "X Ads did not return a campaign ID.")
    line_item = _request("POST", f"{base}/line_items", auth=auth, json={
        "campaign_id": campaign_id, "name": f"{str(payload.get('name') or '').strip()} - line item",
        "product_type": "PROMOTED_TWEETS", "placements": ["TWITTER_TIMELINE"],
        "objective": "WEBSITE_CLICKS", "bid_amount_local_micro": int(_ad_decimal(payload.get("bid") or "1") * 1_000_000),
        "paused": True,
    }).json()
    line_item_id = _created_id(line_item, "data.id")
    if not line_item_id:
        raise HTTPException(502, f"X campaign {campaign_id} exists paused, but no line item ID was returned.")
    try:
        promoted = _request("POST", f"{base}/promoted_tweets", auth=auth, json={
            "line_item_id": line_item_id, "tweet_ids": [tweet_id], "paused": True,
        }).json()
        promoted_id = _created_id(promoted, "data.0.id", "data.id")
    except HTTPException as error:
        raise HTTPException(502, f"X campaign {campaign_id} and line item {line_item_id} remain paused. Promoted Post setup failed: {error.detail}") from error
    return {"ok": True, "provider": "x_ads", "campaign_id": campaign_id, "ad_group_id": line_item_id, "ad_id": promoted_id, "status": "PAUSED"}


def _create_google_demand_gen(account: dict, settings: dict, credentials: dict, payload: dict) -> dict:
    name = str(payload.get("name") or "").strip()
    final_url = str(payload.get("final_url") or "").strip()
    video_id = str(payload.get("youtube_video_id") or "").strip()
    logo_asset = str(payload.get("logo_asset_resource_name") or "").strip()
    headlines = [line.strip() for line in re.split(r"[\n\r]+", str(payload.get("headlines") or "")) if line.strip()]
    long_headline = str(payload.get("long_headline") or "").strip()
    descriptions = [line.strip() for line in re.split(r"[\n\r]+", str(payload.get("descriptions") or "")) if line.strip()]
    if urlparse(final_url).scheme != "https" or not video_id or not logo_asset:
        raise HTTPException(400, "Google Demand Gen needs a public HTTPS landing page, YouTube video ID, and existing Google Ads logo asset resource name.")
    if len(headlines) < 3 or len(headlines) > 5 or not long_headline or not descriptions:
        raise HTTPException(400, "Google Demand Gen needs 3 to 5 headlines, one long headline, and at least one description.")
    if any(len(item) > 40 for item in headlines) or len(long_headline) > 90 or any(len(item) > 90 for item in descriptions):
        raise HTTPException(400, "Short headlines must be at most 40 characters and long headlines and descriptions at most 90 characters.")
    geo_id = str(payload.get("geo_target_constant_id") or "").strip()
    language_id = str(payload.get("language_constant_id") or "1000").strip()
    if not geo_id.isdigit() or not language_id.isdigit():
        raise HTTPException(400, "Enter numeric Google Ads geo target and language constant IDs.")
    budget_micros = int(_ad_decimal(payload.get("daily_budget")) * 1_000_000)
    customer_id, _token, version, headers = _google_headers(account, settings, credentials)
    budget = _request("POST", f"https://googleads.googleapis.com/{version}/customers/{customer_id}/campaignBudgets:mutate", headers=headers, json={
        "operations": [{"create": {"name": f"Revenue360s - {name} - budget", "deliveryMethod": "STANDARD", "amountMicros": str(budget_micros), "explicitlyShared": False}}],
    }).json()
    budget_resource = _created_id(budget, "results.0.resourceName")
    if not budget_resource:
        raise HTTPException(502, "Google Ads did not return the Demand Gen budget resource.")
    campaign = _request("POST", f"https://googleads.googleapis.com/{version}/customers/{customer_id}/campaigns:mutate", headers=headers, json={
        "operations": [{"create": {
            "name": name, "campaignBudget": budget_resource,
            "advertisingChannelType": "DEMAND_GEN", "status": "PAUSED",
            "maximizeConversions": {},
            "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        }}],
    }).json()
    campaign_resource = _created_id(campaign, "results.0.resourceName")
    if not campaign_resource:
        raise HTTPException(502, "Google Ads created the Demand Gen budget but did not return a campaign resource.")
    try:
        ad_group_data = _request("POST", f"https://googleads.googleapis.com/{version}/customers/{customer_id}/adGroups:mutate", headers=headers, json={
            "operations": [{"create": {
                "name": f"{name} - YouTube ad group", "campaign": campaign_resource, "status": "ENABLED",
                "demandGenAdGroupSettings": {"channelControls": {"selectedChannels": {
                    "gmail": False, "discover": False, "display": False,
                    "youtubeInFeed": True, "youtubeInStream": True, "youtubeShorts": True,
                } } },
            }}],
        }).json()
        ad_group_resource = _created_id(ad_group_data, "results.0.resourceName")
        if not ad_group_resource:
            raise HTTPException(502, "Google Ads did not return the Demand Gen ad group resource.")
        criteria = [{"create": {"adGroup": ad_group_resource, "location": {"geoTargetConstant": f"geoTargetConstants/{geo_id}"}}},
                    {"create": {"adGroup": ad_group_resource, "language": {"languageConstant": f"languageConstants/{language_id}"}}}]
        _request("POST", f"https://googleads.googleapis.com/{version}/customers/{customer_id}/adGroupCriteria:mutate", headers=headers, json={"operations": criteria})
        asset = _request("POST", f"https://googleads.googleapis.com/{version}/customers/{customer_id}/assets:mutate", headers=headers, json={
            "operations": [{"create": {"name": f"{name} - YouTube video", "youtubeVideoAsset": {"youtubeVideoId": video_id}}}],
        }).json()
        video_asset = _created_id(asset, "results.0.resourceName")
        if not video_asset:
            raise HTTPException(502, "Google Ads did not return the YouTube video asset resource.")
        ad_response = _request("POST", f"https://googleads.googleapis.com/{version}/customers/{customer_id}/adGroupAds:mutate", headers=headers, json={
            "operations": [{"create": {
                "adGroup": ad_group_resource, "status": "ENABLED",
                "ad": {
                    "name": f"{name} - YouTube video ad", "finalUrls": [final_url],
                    "demandGenVideoResponsiveAd": {
                        "businessName": {"text": str(payload.get("business_name") or name)[:25]},
                        "videos": [{"asset": video_asset}], "logoImages": [{"asset": logo_asset}],
                        "headlines": [{"text": item} for item in headlines],
                        "longHeadlines": [{"text": long_headline}],
                        "descriptions": [{"text": item} for item in descriptions],
                    },
                },
            }}],
        }).json()
    except HTTPException as error:
        raise HTTPException(502, f"Google Demand Gen campaign {campaign_resource.rsplit('/', 1)[-1]} exists paused. YouTube ad setup failed: {error.detail}") from error
    return {"ok": True, "provider": "google_ads", "campaign_id": campaign_resource.rsplit("/", 1)[-1], "resource_name": campaign_resource, "ad_group_resource": ad_group_resource, "ad_resource": _created_id(ad_response, "results.0.resourceName"), "status": "PAUSED", "campaign_type": "DEMAND_GEN", "placement": "YouTube in-feed, in-stream, and Shorts"}


def _microsoft_campaign_response_id(response: bytes, collection_name: str) -> str:
    root = ET.fromstring(response)
    collection = next((item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == collection_name), None)
    if collection is None:
        return ""
    return next((item.text or "" for item in collection if item.text), "")


def _microsoft_append_text_asset(parent: ET.Element, name: str, text: str, namespace: str) -> None:
    xsi = "http://www.w3.org/2001/XMLSchema-instance"
    link = ET.SubElement(parent, f"{{{namespace}}}AssetLink")
    asset = ET.SubElement(link, f"{{{namespace}}}Asset")
    asset.set(f"{{{xsi}}}type", "TextAsset")
    ET.SubElement(asset, f"{{{namespace}}}Name").text = name[:100]
    ET.SubElement(asset, f"{{{namespace}}}Type").text = "Text"
    ET.SubElement(asset, f"{{{namespace}}}Text").text = text


def _create_microsoft_campaign(account: dict, settings: dict, credentials: dict, payload: dict) -> dict:
    account_id = str(account.get("advertiser_id") or "")
    name = str(payload.get("name") or "").strip()
    timezone_name = str(payload.get("time_zone") or settings.get("time_zone") or "").strip()
    location_id = str(payload.get("location_id") or "").strip()
    final_url = str(payload.get("final_url") or "").strip()
    headlines = [line.strip() for line in re.split(r"[\n\r]+", str(payload.get("headlines") or "")) if line.strip()]
    descriptions = [line.strip() for line in re.split(r"[\n\r]+", str(payload.get("descriptions") or "")) if line.strip()]
    keywords = list(dict.fromkeys(item.strip() for item in re.split(r"[,\n\r]+", str(payload.get("keywords") or "")) if item.strip()))
    if not account_id or not timezone_name or not location_id.isdigit() or urlparse(final_url).scheme != "https":
        raise HTTPException(400, "Microsoft Ads needs a valid Account ID, API time zone, numeric location ID, and public HTTPS landing page.")
    if not 3 <= len(headlines) <= 15 or any(len(value) > 30 for value in headlines):
        raise HTTPException(400, "Add 3 to 15 Microsoft responsive search ad headlines, each 30 characters or fewer.")
    if not 2 <= len(descriptions) <= 4 or any(len(value) > 90 for value in descriptions):
        raise HTTPException(400, "Add 2 to 4 Microsoft responsive search ad descriptions, each 90 characters or fewer.")
    if not keywords or any(len(value) > 100 for value in keywords):
        raise HTTPException(400, "Add at least one Microsoft Ads keyword, each 100 characters or fewer.")
    budget = _ad_decimal(payload.get("daily_budget"))
    cpc = _ad_decimal(payload.get("max_cpc") or "1", "max CPC")
    namespace = "https://bingads.microsoft.com/CampaignManagement/v13"
    xsi = "http://www.w3.org/2001/XMLSchema-instance"
    arrays = "http://schemas.microsoft.com/2003/10/Serialization/Arrays"
    ET.register_namespace("r", namespace)

    campaign_request = ET.Element(f"{{{namespace}}}AddCampaignsRequest")
    ET.SubElement(campaign_request, f"{{{namespace}}}AccountId").text = account_id
    campaigns_node = ET.SubElement(campaign_request, f"{{{namespace}}}Campaigns")
    campaign_node = ET.SubElement(campaigns_node, f"{{{namespace}}}Campaign")
    campaign_node.set(f"{{{xsi}}}type", "SearchCampaign")
    for field, value in (("Name", name), ("BudgetType", "DailyBudgetStandard"), ("DailyBudget", str(budget)), ("TimeZone", timezone_name), ("CampaignType", "Search"), ("Status", "Paused")):
        ET.SubElement(campaign_node, f"{{{namespace}}}{field}").text = value
    language_list = ET.SubElement(campaign_node, f"{{{namespace}}}Languages")
    ET.SubElement(language_list, f"{{{arrays}}}string").text = str(payload.get("language") or "English")
    try:
        campaign_response = _microsoft_soap_request(account, credentials, settings, "CampaignManagement", "AddCampaigns", campaign_request)
        campaign_id = _microsoft_campaign_response_id(campaign_response, "CampaignIds")
    except HTTPException:
        raise
    if not campaign_id:
        raise HTTPException(502, "Microsoft Ads did not return a campaign ID.")

    try:
        target_request = ET.Element(f"{{{namespace}}}AddCampaignCriterionsRequest")
        criteria = ET.SubElement(target_request, f"{{{namespace}}}CampaignCriterions")
        criterion = ET.SubElement(criteria, f"{{{namespace}}}CampaignCriterion")
        criterion.set(f"{{{xsi}}}type", "r:BiddableCampaignCriterion")
        ET.SubElement(criterion, f"{{{namespace}}}CampaignId").text = campaign_id
        location = ET.SubElement(criterion, f"{{{namespace}}}Criterion")
        location.set(f"{{{xsi}}}type", "r:LocationCriterion")
        ET.SubElement(location, f"{{{namespace}}}LocationId").text = location_id
        ET.SubElement(target_request, f"{{{namespace}}}CriterionType").text = "Targets"
        _microsoft_soap_request(account, credentials, settings, "CampaignManagement", "AddCampaignCriterions", target_request)

        group_request = ET.Element(f"{{{namespace}}}AddAdGroupsRequest")
        ET.SubElement(group_request, f"{{{namespace}}}CampaignId").text = campaign_id
        groups = ET.SubElement(group_request, f"{{{namespace}}}AdGroups")
        group = ET.SubElement(groups, f"{{{namespace}}}AdGroup")
        ET.SubElement(group, f"{{{namespace}}}Name").text = f"{name} - ad group"
        bid_node = ET.SubElement(group, f"{{{namespace}}}CpcBid")
        ET.SubElement(bid_node, f"{{{namespace}}}Amount").text = str(cpc)
        ET.SubElement(group, f"{{{namespace}}}Language").text = str(payload.get("language") or "English")
        ET.SubElement(group, f"{{{namespace}}}Network").text = "OwnedAndOperatedAndSyndicatedSearch"
        ET.SubElement(group, f"{{{namespace}}}Status").text = "Paused"
        group_response = _microsoft_soap_request(account, credentials, settings, "CampaignManagement", "AddAdGroups", group_request)
        ad_group_id = _microsoft_campaign_response_id(group_response, "AdGroupIds")
        if not ad_group_id:
            raise HTTPException(502, "Microsoft Ads did not return an ad group ID.")

        keyword_request = ET.Element(f"{{{namespace}}}AddKeywordsRequest")
        ET.SubElement(keyword_request, f"{{{namespace}}}AdGroupId").text = ad_group_id
        keyword_nodes = ET.SubElement(keyword_request, f"{{{namespace}}}Keywords")
        match_type = str(payload.get("match_type") or "Phrase").capitalize()
        if match_type not in {"Broad", "Phrase", "Exact"}:
            raise HTTPException(400, "Microsoft keyword match type must be Broad, Phrase, or Exact.")
        for keyword in keywords:
            keyword_node = ET.SubElement(keyword_nodes, f"{{{namespace}}}Keyword")
            keyword_bid = ET.SubElement(keyword_node, f"{{{namespace}}}Bid")
            ET.SubElement(keyword_bid, f"{{{namespace}}}Amount").text = str(cpc)
            ET.SubElement(keyword_node, f"{{{namespace}}}MatchType").text = match_type
            ET.SubElement(keyword_node, f"{{{namespace}}}Text").text = keyword
        _microsoft_soap_request(account, credentials, settings, "CampaignManagement", "AddKeywords", keyword_request)

        ad_request = ET.Element(f"{{{namespace}}}AddAdsRequest")
        ET.SubElement(ad_request, f"{{{namespace}}}AdGroupId").text = ad_group_id
        ads_node = ET.SubElement(ad_request, f"{{{namespace}}}Ads")
        ad_node = ET.SubElement(ads_node, f"{{{namespace}}}Ad")
        ad_node.set(f"{{{xsi}}}type", "ResponsiveSearchAd")
        ET.SubElement(ad_node, f"{{{namespace}}}FinalUrls").append(ET.Element(f"{{{arrays}}}string"))
        list(ad_node)[-1][0].text = final_url
        ET.SubElement(ad_node, f"{{{namespace}}}Status").text = "Paused"
        ET.SubElement(ad_node, f"{{{namespace}}}Type").text = "ResponsiveSearch"
        headlines_node = ET.SubElement(ad_node, f"{{{namespace}}}Headlines")
        for index, value in enumerate(headlines, start=1):
            _microsoft_append_text_asset(headlines_node, f"{name} headline {index}", value, namespace)
        descriptions_node = ET.SubElement(ad_node, f"{{{namespace}}}Descriptions")
        for index, value in enumerate(descriptions, start=1):
            _microsoft_append_text_asset(descriptions_node, f"{name} description {index}", value, namespace)
        ET.SubElement(ad_node, f"{{{namespace}}}Path1").text = str(payload.get("path1") or "")[:15]
        ET.SubElement(ad_node, f"{{{namespace}}}Path2").text = str(payload.get("path2") or "")[:15]
        ad_response = _microsoft_soap_request(account, credentials, settings, "CampaignManagement", "AddAds", ad_request)
        ad_id = _microsoft_campaign_response_id(ad_response, "AdIds")
        if not ad_id:
            raise HTTPException(502, "Microsoft Ads did not return an ad ID.")
    except HTTPException as error:
        raise HTTPException(502, f"Microsoft Ads campaign {campaign_id} remains paused. Ad group or creative setup failed: {error.detail}") from error
    return {"ok": True, "provider": "microsoft_ads", "campaign_id": campaign_id, "ad_group_id": ad_group_id, "ad_id": ad_id, "status": "PAUSED"}


@router.post("/platform-accounts/{account_id}/campaigns")
def create_campaign(account_id: str, payload: dict, user: dict = Depends(user_from_header)) -> dict:
    table, account = _account(user["workspace_id"], account_id)
    if not table or not account:
        raise HTTPException(404, "Ad account not found in this workspace.")
    platform = account.get("platform") or "meta_ads"
    settings = _settings(account)
    credentials = _credentials(table, account)
    if platform == "meta_ads":
        return _create_meta_campaign(account, credentials, payload)
    if platform == "linkedin_ads":
        return _create_linkedin_campaign(account, settings, credentials, payload)
    if platform == "tiktok_ads":
        return _create_tiktok_campaign(account, settings, credentials, payload)
    if platform == "pinterest_ads":
        return _create_pinterest_campaign(account, settings, credentials, payload)
    if platform == "snapchat_ads":
        return _create_snapchat_campaign(account, credentials, payload)
    if platform == "x_ads":
        return _create_x_campaign(account, settings, credentials, payload)
    if platform == "microsoft_ads":
        return _create_microsoft_campaign(account, settings, credentials, payload)
    if platform != "google_ads":
        raise HTTPException(400, "This advertising provider is not supported.")
    if str(payload.get("campaign_type") or "SEARCH").upper() in {"DEMAND_GEN", "YOUTUBE"}:
        return _create_google_demand_gen(account, settings, credentials, payload)
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 128:
        raise HTTPException(400, "Enter a campaign name of 1 to 128 characters.")
    final_url = str(payload.get("final_url") or "").strip()
    parsed_url = urlparse(final_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise HTTPException(400, "The final URL must be a public HTTPS URL.")
    headlines = [line.strip() for line in re.split(r"[\n\r]+", str(payload.get("headlines") or "")) if line.strip()]
    descriptions = [line.strip() for line in re.split(r"[\n\r]+", str(payload.get("descriptions") or "")) if line.strip()]
    keywords = list(dict.fromkeys(item.strip() for item in re.split(r"[,\n\r]+", str(payload.get("keywords") or "")) if item.strip()))
    if not 3 <= len(headlines) <= 15 or any(len(item) > 30 for item in headlines):
        raise HTTPException(400, "Add 3 to 15 ad headlines, each 30 characters or fewer.")
    if not 2 <= len(descriptions) <= 4 or any(len(item) > 90 for item in descriptions):
        raise HTTPException(400, "Add 2 to 4 ad descriptions, each 90 characters or fewer.")
    if not keywords or len(keywords) > 100 or any(len(item) > 80 for item in keywords):
        raise HTTPException(400, "Add 1 to 100 keywords, each 80 characters or fewer.")
    geo_target_id = str(payload.get("geo_target_constant_id") or "").strip()
    language_id = str(payload.get("language_constant_id") or "").strip()
    if not geo_target_id.isdigit() or not language_id.isdigit():
        raise HTTPException(400, "Enter numeric Google Ads geo target and language constant IDs.")
    match_type = str(payload.get("match_type") or "EXACT").upper()
    if match_type not in {"EXACT", "PHRASE", "BROAD"}:
        raise HTTPException(400, "Keyword match type must be EXACT, PHRASE, or BROAD.")
    try:
        budget_micros = int(Decimal(str(payload.get("daily_budget") or "0")) * 1_000_000)
        cpc_bid_micros = int(Decimal(str(payload.get("max_cpc") or "0")) * 1_000_000)
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(400, "Enter valid daily budget and max CPC amounts in the customer account currency.")
    if budget_micros <= 0 or cpc_bid_micros <= 0:
        raise HTTPException(400, "Daily budget and max CPC must be greater than zero.")
    customer_id, _token, version, headers = _google_headers(account, settings, credentials)
    budget_response = _request(
        "POST",
        f"https://googleads.googleapis.com/{version}/customers/{customer_id}/campaignBudgets:mutate",
        headers=headers,
        json={"operations": [{"create": {
            "name": f"Revenue360s - {name} - budget",
            "deliveryMethod": "STANDARD",
            "amountMicros": str(budget_micros),
            "explicitlyShared": False,
        }}]},
    ).json()
    budget_resource = str((((budget_response.get("results") or [{}])[0]).get("resourceName")) or "")
    if not budget_resource:
        raise HTTPException(502, "Google Ads did not return the new campaign budget resource.")
    try:
        campaign_response = _request(
            "POST",
            f"https://googleads.googleapis.com/{version}/customers/{customer_id}/campaigns:mutate",
            headers=headers,
            json={"operations": [{"create": {
                "name": name,
                "campaignBudget": budget_resource,
                "advertisingChannelType": "SEARCH",
                "status": "PAUSED",
                "manualCpc": {},
                "networkSettings": {
                    "targetGoogleSearch": True,
                    "targetSearchNetwork": True,
                    "targetContentNetwork": False,
                    "targetPartnerSearchNetwork": False,
                },
            }}]},
        ).json()
    except HTTPException:
        try:
            _request(
                "POST",
                f"https://googleads.googleapis.com/{version}/customers/{customer_id}/campaignBudgets:mutate",
                headers=headers,
                json={"operations": [{"remove": budget_resource}]},
            )
        except HTTPException:
            pass
        raise
    resource = str((((campaign_response.get("results") or [{}])[0]).get("resourceName")) or "")
    if not resource:
        raise HTTPException(502, "Google Ads created a budget but did not return a campaign resource.")
    try:
        criteria = [{"create": {
            "campaign": resource,
            "location": {"geoTargetConstant": f"geoTargetConstants/{geo_target_id}"},
        }}, {"create": {
            "campaign": resource,
            "language": {"languageConstant": f"languageConstants/{language_id}"},
        }}]
        _request("POST", f"https://googleads.googleapis.com/{version}/customers/{customer_id}/campaignCriteria:mutate", headers=headers, json={"operations": criteria})
        ad_group_result = _request(
            "POST", f"https://googleads.googleapis.com/{version}/customers/{customer_id}/adGroups:mutate",
            headers=headers,
            json={"operations": [{"create": {
                "name": f"{name} - ad group",
                "campaign": resource,
                "status": "ENABLED",
                "type": "SEARCH_STANDARD",
                "cpcBidMicros": str(cpc_bid_micros),
            }}]},
        ).json()
        ad_group_resource = str((((ad_group_result.get("results") or [{}])[0]).get("resourceName")) or "")
        if not ad_group_resource:
            raise HTTPException(502, "Google Ads did not return the new ad group resource.")
        keyword_operations = [{"create": {
            "adGroup": ad_group_resource,
            "status": "ENABLED",
            "keyword": {"text": keyword, "matchType": match_type},
        }} for keyword in keywords]
        _request("POST", f"https://googleads.googleapis.com/{version}/customers/{customer_id}/adGroupCriteria:mutate", headers=headers, json={"operations": keyword_operations})
        ad_result = _request(
            "POST", f"https://googleads.googleapis.com/{version}/customers/{customer_id}/adGroupAds:mutate",
            headers=headers,
            json={"operations": [{"create": {
                "adGroup": ad_group_resource,
                "status": "ENABLED",
                "ad": {
                    "finalUrls": [final_url],
                    "responsiveSearchAd": {
                        "headlines": [{"text": item} for item in headlines],
                        "descriptions": [{"text": item} for item in descriptions],
                    },
                },
            }}]},
        ).json()
    except HTTPException as error:
        raise HTTPException(502, f"Google Ads campaign {resource.rsplit('/', 1)[-1]} exists but remains paused. Ad setup failed: {error.detail}") from error
    ad_resource = str((((ad_result.get("results") or [{}])[0]).get("resourceName")) or "")
    return {
        "ok": True, "provider": "google_ads", "campaign_id": resource.rsplit("/", 1)[-1],
        "resource_name": resource, "status": "PAUSED", "budget_resource": budget_resource,
        "ad_group_resource": ad_group_resource, "ad_resource": ad_resource,
    }


@router.post("/platform-accounts/{account_id}/campaigns/{campaign_id}/budget")
def update_campaign_budget(account_id: str, campaign_id: str, payload: dict, user: dict = Depends(user_from_header)) -> dict:
    table, account = _account(user["workspace_id"], account_id)
    if not table or not account:
        raise HTTPException(404, "Ad account not found in this workspace.")
    platform = account.get("platform") or "meta_ads"
    settings = _settings(account)
    credentials = _credentials(table, account)
    if platform != "google_ads":
        amount = _ad_decimal(payload.get("daily_budget"))
        if platform == "meta_ads":
            sub_entity_id = str(payload.get("ad_group_id") or payload.get("adset_id") or "").strip()
            token = credentials.get("access_token") or ""
            if not sub_entity_id or not token:
                raise HTTPException(400, "Meta ad set budgets require the ad set ID and Marketing API token.")
            version = env("META_GRAPH_API_VERSION", "v25.0")
            result = _request("POST", f"https://graph.facebook.com/{version}/{quote(sub_entity_id, safe='')}", data={"daily_budget": str(int(amount * 100)), "access_token": token}).json()
        elif platform == "linkedin_ads":
            currency = str(payload.get("currency_code") or settings.get("currency_code") or "USD").upper()
            headers = _linkedin_headers(settings, credentials)
            headers["X-RestLi-Method"] = "PARTIAL_UPDATE"
            result = _request("POST", f"https://api.linkedin.com/rest/adAccounts/{quote(str(account.get('advertiser_id') or ''), safe='')}/adCampaigns/{quote(campaign_id, safe='')}", headers=headers, json={"patch": {"$set": {"dailyBudget": {"amount": str(amount), "currencyCode": currency}}}}).json()
        elif platform == "tiktok_ads":
            sub_entity_id = str(payload.get("ad_group_id") or "").strip()
            advertiser_id = str(account.get("advertiser_id") or "")
            token = credentials.get("access_token") or ""
            if not sub_entity_id or not token or not advertiser_id:
                raise HTTPException(400, "TikTok ad group budgets require the ad group ID and Marketing API token.")
            version = str(settings.get("api_version") or "v1.3").strip("/")
            result = _tiktok_response_data(_request("POST", f"https://business-api.tiktok.com/open_api/{version}/adgroup/update/", headers={"Access-Token": token}, json={"advertiser_id": advertiser_id, "adgroup_id": sub_entity_id, "budget_mode": "BUDGET_MODE_DAY", "budget": float(amount)}))
        elif platform == "x_ads":
            auth = _x_oauth(credentials)
            version = str(settings.get("api_version") or "12").strip("/")
            result = _request("POST", f"https://ads-api.x.com/{version}/accounts/{quote(str(account.get('advertiser_id') or ''), safe='')}/campaigns/{quote(campaign_id, safe='')}", auth=auth, json={"daily_budget_amount_local_micro": int(amount * 1_000_000)}).json()
        elif platform == "microsoft_ads":
            row = next((item for item in _microsoft_campaigns(account, credentials, settings, include_report=False) if str(item.get("id")) == str(campaign_id)), None)
            if not row or not row.get("provider_xml"):
                raise HTTPException(404, "Microsoft Ads campaign was not found in this account.")
            provider_campaign = ET.fromstring(row["provider_xml"])
            budget_field = next((child for child in provider_campaign if child.tag.rsplit("}", 1)[-1] == "DailyBudget"), None)
            if budget_field is None:
                raise HTTPException(409, "Microsoft Ads did not return a daily budget field for this campaign.")
            budget_field.text = str(amount)
            _microsoft_update_campaign(account, settings, credentials, provider_campaign)
            result = {"campaign_id": campaign_id}
        elif platform == "pinterest_ads":
            token = _refresh_pinterest(credentials)
            if not token:
                raise HTTPException(400, "Pinterest Ads needs an API access token.")
            version = str(settings.get("api_version") or "v5").strip("/")
            result = _request("PATCH", f"https://api.pinterest.com/{version}/ad_accounts/{quote(str(account.get('advertiser_id') or ''), safe='')}/campaigns", headers={"Authorization": f"Bearer {token}"}, json=[{"id": campaign_id, "daily_spend_cap": int(amount * 1_000_000)}]).json()
        elif platform == "snapchat_ads":
            token = _refresh_snapchat(credentials)
            if not token:
                raise HTTPException(400, "Snap Ads needs a Marketing API access token.")
            result = _request("PATCH", f"https://adsapi.snapchat.com/v1/campaigns/{quote(campaign_id, safe='')}", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json-patch+json"}, json=[{"op": "replace", "path": "/daily_budget_micro", "value": int(amount * 1_000_000)}]).json()
        else:
            raise HTTPException(400, "This provider does not support budget edits through this connector.")
        return {"ok": True, "provider": platform, "campaign_id": campaign_id, "daily_budget": float(amount), "result": result}
    if not str(campaign_id).isdigit():
        raise HTTPException(400, "Google Ads campaign IDs must be numeric.")
    try:
        budget_micros = int(Decimal(str(payload.get("daily_budget") or "0")) * 1_000_000)
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(400, "Enter a valid daily budget in the customer account currency.")
    if budget_micros <= 0:
        raise HTTPException(400, "Daily budget must be greater than zero.")
    customer_id, _token, version, headers = _google_headers(account, settings, credentials)
    campaign_rows = _request(
        "POST", f"https://googleads.googleapis.com/{version}/customers/{customer_id}/googleAds:searchStream",
        headers=headers,
        json={"query": f"SELECT campaign.id, campaign_budget.resource_name, campaign_budget.explicitly_shared FROM campaign WHERE campaign.id = {campaign_id} AND campaign.status != 'REMOVED' LIMIT 1"},
    ).json()
    rows = [row for batch in campaign_rows if isinstance(campaign_rows, list) for row in batch.get("results", [])]
    if not rows and isinstance(campaign_rows, dict):
        rows = campaign_rows.get("results") or []
    if not rows:
        raise HTTPException(404, "Google Ads campaign was not found.")
    budget_data = rows[0].get("campaignBudget") or {}
    budget_resource = str(budget_data.get("resourceName") or "")
    if not budget_resource:
        raise HTTPException(502, "Google Ads did not return this campaign's budget resource.")
    if budget_data.get("explicitlyShared") is True:
        raise HTTPException(409, "This campaign uses a shared Google Ads budget. Edit it in Google Ads to avoid changing other campaigns.")
    shared_campaigns = _request(
        "POST", f"https://googleads.googleapis.com/{version}/customers/{customer_id}/googleAds:searchStream",
        headers=headers,
        json={"query": f"SELECT campaign.id FROM campaign WHERE campaign_budget.resource_name = '{budget_resource}' AND campaign.status != 'REMOVED' LIMIT 2"},
    ).json()
    shared_rows = [row for batch in shared_campaigns if isinstance(shared_campaigns, list) for row in batch.get("results", [])]
    if not shared_rows and isinstance(shared_campaigns, dict):
        shared_rows = shared_campaigns.get("results") or []
    if len(shared_rows) > 1:
        raise HTTPException(409, "This Google Ads budget is assigned to multiple campaigns and cannot be edited safely here.")
    result = _request(
        "POST", f"https://googleads.googleapis.com/{version}/customers/{customer_id}/campaignBudgets:mutate",
        headers=headers,
        json={"operations": [{"update": {"resourceName": budget_resource, "amountMicros": str(budget_micros)}, "updateMask": "amountMicros"}]},
    ).json()
    return {"ok": True, "provider": "google_ads", "campaign_id": campaign_id, "daily_budget": budget_micros / 1_000_000, "result": result}


@router.post("/platform-accounts/{account_id}/campaigns/{campaign_id}/targeting")
def update_campaign_targeting(account_id: str, campaign_id: str, payload: dict, user: dict = Depends(user_from_header)) -> dict:
    table, account = _account(user["workspace_id"], account_id)
    if not table or not account:
        raise HTTPException(404, "Ad account not found in this workspace.")
    platform = account.get("platform") or "meta_ads"
    settings = _settings(account)
    credentials = _credentials(table, account)
    targeting = payload.get("targeting")
    if not isinstance(targeting, dict) or not targeting:
        raise HTTPException(400, "Enter the provider's targeting settings as a non-empty JSON object.")
    if platform == "google_ads":
        geo_ids = targeting.get("geo_target_constant_ids") or []
        language_ids = targeting.get("language_constant_ids") or []
        if not isinstance(geo_ids, list) or not isinstance(language_ids, list) or not geo_ids and not language_ids:
            raise HTTPException(400, "Google targeting needs geo_target_constant_ids and/or language_constant_ids arrays.")
        if any(not str(value).isdigit() for value in [*geo_ids, *language_ids]):
            raise HTTPException(400, "Google geo and language target IDs must be numeric.")
        if not str(campaign_id).isdigit():
            raise HTTPException(400, "Google Ads campaign IDs must be numeric.")
        customer_id, _token, version, headers = _google_headers(account, settings, credentials)
        campaign_resource = f"customers/{customer_id}/campaigns/{campaign_id}"
        existing = _google_search_rows(
            customer_id, version, headers,
            "SELECT campaign_criterion.resource_name FROM campaign_criterion "
            f"WHERE campaign.id = {campaign_id} AND campaign_criterion.type IN ('LOCATION', 'LANGUAGE')",
        )
        operations = [{"remove": row.get("campaignCriterion", {}).get("resourceName")} for row in existing if row.get("campaignCriterion", {}).get("resourceName")]
        operations.extend({"create": {"campaign": campaign_resource, "location": {"geoTargetConstant": f"geoTargetConstants/{value}"}}} for value in geo_ids)
        operations.extend({"create": {"campaign": campaign_resource, "language": {"languageConstant": f"languageConstants/{value}"}}} for value in language_ids)
        result = _request("POST", f"https://googleads.googleapis.com/{version}/customers/{customer_id}/campaignCriteria:mutate", headers=headers, json={"operations": operations}).json()
    elif platform == "meta_ads":
        adset_id = str(payload.get("ad_group_id") or payload.get("adset_id") or "").strip()
        token = credentials.get("access_token") or ""
        if not adset_id or not token:
            raise HTTPException(400, "Meta targeting is set on the ad set. Enter its ID and an ads_management token.")
        version = env("META_GRAPH_API_VERSION", "v25.0")
        result = _request("POST", f"https://graph.facebook.com/{version}/{quote(adset_id, safe='')}", data={"targeting": json.dumps(targeting), "access_token": token}).json()
    elif platform == "linkedin_ads":
        headers = _linkedin_headers(settings, credentials)
        headers["X-RestLi-Method"] = "PARTIAL_UPDATE"
        result = _request("POST", f"https://api.linkedin.com/rest/adAccounts/{quote(str(account.get('advertiser_id') or ''), safe='')}/adCampaigns/{quote(campaign_id, safe='')}", headers=headers, json={"patch": {"$set": {"targetingCriteria": targeting}}}).json()
    elif platform == "tiktok_ads":
        adgroup_id = str(payload.get("ad_group_id") or "").strip()
        advertiser_id = str(account.get("advertiser_id") or "")
        token = credentials.get("access_token") or ""
        if not adgroup_id or not advertiser_id or not token:
            raise HTTPException(400, "TikTok targeting is set on the ad group. Enter its ID and Marketing API token.")
        allowed = {"location_ids", "age_groups", "gender", "languages", "interest_category_ids", "audience_ids", "excluded_audience_ids", "placement_type", "placements"}
        fields = {key: value for key, value in targeting.items() if key in allowed}
        if not fields or len(fields) != len(targeting):
            raise HTTPException(400, f"TikTok targeting fields must use: {', '.join(sorted(allowed))}.")
        version = str(settings.get("api_version") or "v1.3").strip("/")
        result = _tiktok_response_data(_request("POST", f"https://business-api.tiktok.com/open_api/{version}/adgroup/update/", headers={"Access-Token": token}, json={"advertiser_id": advertiser_id, "adgroup_id": adgroup_id, **fields}))
    elif platform == "x_ads":
        line_item_id = str(payload.get("ad_group_id") or payload.get("line_item_id") or "").strip()
        criteria = targeting.get("criteria")
        if not line_item_id or not isinstance(criteria, list) or not criteria:
            raise HTTPException(400, "X targeting uses separate line-item criteria. Enter line_item_id and a criteria array of {targeting_type, targeting_value} objects.")
        auth = _x_oauth(credentials)
        version = str(settings.get("api_version") or "12").strip("/")
        base = f"https://ads-api.x.com/{version}/accounts/{quote(str(account.get('advertiser_id') or ''), safe='')}/targeting_criteria"
        results = []
        for criterion in criteria:
            if not isinstance(criterion, dict) or not criterion.get("targeting_type") or not criterion.get("targeting_value"):
                raise HTTPException(400, "Each X targeting criterion needs targeting_type and targeting_value.")
            results.append(_request("POST", base, auth=auth, json={"line_item_id": line_item_id, "targeting_type": criterion["targeting_type"], "targeting_value": criterion["targeting_value"]}).json())
        result = results
    elif platform == "microsoft_ads":
        location_ids = targeting.get("location_ids") or []
        if not str(campaign_id).isdigit() or not isinstance(location_ids, list) or not location_ids or any(not str(value).isdigit() for value in location_ids):
            raise HTTPException(400, "Microsoft Ads targeting updates require a location_ids array of numeric location IDs.")
        namespace = "https://bingads.microsoft.com/CampaignManagement/v13"
        xsi = "http://www.w3.org/2001/XMLSchema-instance"
        arrays = "http://schemas.microsoft.com/2003/10/Serialization/Arrays"
        current_request = ET.Element(f"{{{namespace}}}GetCampaignCriterionsByIdsRequest")
        no_ids = ET.SubElement(current_request, f"{{{namespace}}}CampaignCriterionIds")
        no_ids.set(f"{{{xsi}}}nil", "true")
        ET.SubElement(current_request, f"{{{namespace}}}CampaignId").text = str(campaign_id)
        ET.SubElement(current_request, f"{{{namespace}}}CriterionType").text = "Location"
        current_response = ET.fromstring(_microsoft_soap_request(account, credentials, settings, "CampaignManagement", "GetCampaignCriterionsByIds", current_request))
        existing_location_ids = [
            node.findtext(f"{{{namespace}}}Id") for node in current_response.iter()
            if node.tag.rsplit("}", 1)[-1] == "CampaignCriterion" and node.findtext(f"{{{namespace}}}Id")
        ]
        if existing_location_ids:
            delete_request = ET.Element(f"{{{namespace}}}DeleteCampaignCriterionsRequest")
            delete_ids = ET.SubElement(delete_request, f"{{{namespace}}}CampaignCriterionIds")
            for criterion_id in existing_location_ids:
                ET.SubElement(delete_ids, f"{{{arrays}}}long").text = criterion_id
            ET.SubElement(delete_request, f"{{{namespace}}}CampaignId").text = str(campaign_id)
            ET.SubElement(delete_request, f"{{{namespace}}}CriterionType").text = "Targets"
            _microsoft_soap_request(account, credentials, settings, "CampaignManagement", "DeleteCampaignCriterions", delete_request)
        request_node = ET.Element(f"{{{namespace}}}AddCampaignCriterionsRequest")
        criteria_node = ET.SubElement(request_node, f"{{{namespace}}}CampaignCriterions")
        for location_id in location_ids:
            criterion = ET.SubElement(criteria_node, f"{{{namespace}}}CampaignCriterion")
            criterion.set(f"{{{xsi}}}type", "r:BiddableCampaignCriterion")
            ET.SubElement(criterion, f"{{{namespace}}}CampaignId").text = campaign_id
            location = ET.SubElement(criterion, f"{{{namespace}}}Criterion")
            location.set(f"{{{xsi}}}type", "r:LocationCriterion")
            ET.SubElement(location, f"{{{namespace}}}LocationId").text = str(location_id)
        ET.SubElement(request_node, f"{{{namespace}}}CriterionType").text = "Targets"
        result = _microsoft_soap_request(account, credentials, settings, "CampaignManagement", "AddCampaignCriterions", request_node).decode("utf-8", errors="replace")
    elif platform == "pinterest_ads":
        ad_group_id = str(payload.get("ad_group_id") or "").strip()
        token = _refresh_pinterest(credentials)
        if not ad_group_id or not token:
            raise HTTPException(400, "Pinterest targeting is set on the ad group. Enter its ID and Ads API token.")
        version = str(settings.get("api_version") or "v5").strip("/")
        result = _request("PATCH", f"https://api.pinterest.com/{version}/ad_accounts/{quote(str(account.get('advertiser_id') or ''), safe='')}/ad_groups", headers={"Authorization": f"Bearer {token}"}, json=[{"id": ad_group_id, "targeting_spec": targeting}]).json()
    elif platform == "snapchat_ads":
        squad_id = str(payload.get("ad_group_id") or "").strip()
        token = _refresh_snapchat(credentials)
        if not squad_id or not token:
            raise HTTPException(400, "Snap targeting is set on the ad squad. Enter its ID and Marketing API token.")
        headers = {"Authorization": f"Bearer {token}"}
        base = "https://adsapi.snapchat.com/v1"
        squad_response = _request("GET", f"{base}/adsquads/{quote(squad_id, safe='')}", headers=headers, params={"targeting_v2": "ENABLED"}).json()
        squad = ((squad_response.get("adsquads") or [{}])[0]).get("adsquad") or {}
        if not squad:
            raise HTTPException(404, "Snap did not return the requested ad squad.")
        squad["targeting"] = targeting
        result = _request("PUT", f"{base}/campaigns/{quote(campaign_id, safe='')}/adsquads", headers=headers, json={"adsquads": [squad]}).json()
    else:
        raise HTTPException(400, "This advertising provider does not support targeting edits through this connector.")
    return {"ok": True, "provider": platform, "campaign_id": campaign_id, "result": result}


@router.get("/optimization-rules")
def list_optimization_rules(user: dict = Depends(user_from_header)) -> list[dict]:
    return query(
        "SELECT r.*, COALESCE(a.name, m.name) AS account_name, COALESCE(a.advertiser_id, m.ad_account_id) AS advertiser_id "
        "FROM ad_optimization_rules r "
        "LEFT JOIN ad_network_accounts a ON a.id = r.account_id AND a.workspace_id = r.workspace_id "
        "LEFT JOIN meta_ad_accounts m ON m.id = r.account_id AND m.workspace_id = r.workspace_id "
        "WHERE r.workspace_id = ? ORDER BY r.created_at DESC",
        (user["workspace_id"],),
    )


@router.post("/platform-accounts/{account_id}/campaigns/{campaign_id}/optimization-rule")
def save_optimization_rule(account_id: str, campaign_id: str, payload: dict, user: dict = Depends(user_from_header)) -> dict:
    table, account = _account(user["workspace_id"], account_id)
    if not table or not account:
        raise HTTPException(404, "Ad account not found in this workspace.")
    platform = account.get("platform") or "meta_ads"
    if platform not in PROVIDERS:
        raise HTTPException(400, "This ad account does not support campaign optimization rules.")
    if not campaign_id.strip():
        raise HTTPException(400, "A campaign ID is required.")
    try:
        minimum_spend = Decimal(str(payload.get("minimum_30d_spend") or "0"))
        conversion_limit_enabled = bool(payload.get("conversion_limit_enabled", payload.get("maximum_30d_conversions") not in (None, "")))
        maximum_conversions = Decimal(str(payload.get("maximum_30d_conversions") or "0")) if conversion_limit_enabled else Decimal(0)
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(400, "Enter a numeric 30-day spend limit and, optionally, a conversion limit.")
    if not minimum_spend.is_finite() or not maximum_conversions.is_finite() or minimum_spend <= 0 or maximum_conversions < 0:
        raise HTTPException(400, "Spend threshold must be greater than zero and maximum conversions cannot be negative.")
    existing = one(
        "SELECT id FROM ad_optimization_rules WHERE workspace_id = ? AND account_id = ? AND campaign_id = ?",
        (user["workspace_id"], account_id, campaign_id),
    )
    values = {
        "minimum_30d_spend": minimum_spend,
        "maximum_30d_conversions": maximum_conversions,
        "conversion_limit_enabled": 1 if conversion_limit_enabled else 0,
        "enabled": 1,
        "last_checked_at": "",
        "last_action": "",
        "last_detail": "",
        "updated_at": now_iso(),
    }
    if existing:
        execute(
            "UPDATE ad_optimization_rules SET minimum_30d_spend = ?, maximum_30d_conversions = ?, conversion_limit_enabled = ?, enabled = 1, last_checked_at = '', last_action = '', last_detail = '', updated_at = ? WHERE id = ? AND workspace_id = ?",
            (minimum_spend, maximum_conversions, values["conversion_limit_enabled"], values["updated_at"], existing["id"], user["workspace_id"]),
        )
        rule_id = existing["id"]
    else:
        rule_id = insert(
            "ad_optimization_rules",
            {"account_id": account_id, "campaign_id": campaign_id, **values},
            user["workspace_id"],
        )
    return {"ok": True, "id": rule_id, "enabled": True}


@router.post("/optimization-rules/{rule_id}/enabled")
def set_optimization_rule_enabled(rule_id: str, payload: dict, user: dict = Depends(user_from_header)) -> dict:
    existing = one("SELECT id FROM ad_optimization_rules WHERE id = ? AND workspace_id = ?", (rule_id, user["workspace_id"]))
    if not existing:
        raise HTTPException(404, "Optimization rule not found.")
    enabled = 1 if payload.get("enabled") else 0
    execute("UPDATE ad_optimization_rules SET enabled = ?, updated_at = ? WHERE id = ? AND workspace_id = ?", (enabled, now_iso(), rule_id, user["workspace_id"]))
    return {"ok": True, "enabled": bool(enabled)}


def run_optimization_rules(limit: int = 100) -> dict:
    rules = query(
        "SELECT * FROM ad_optimization_rules WHERE enabled = 1 AND "
        "(last_checked_at = '' OR last_checked_at <= DATE_FORMAT(UTC_TIMESTAMP() - INTERVAL 1 DAY, '%Y-%m-%dT%H:%i:%s+00:00')) "
        "ORDER BY created_at ASC LIMIT ?",
        (limit,),
    )
    campaigns_by_account: dict[tuple[str, str], dict[str, dict]] = {}
    checked = paused = failed = 0
    for rule in rules:
        cache_key = (rule["workspace_id"], rule["account_id"])
        try:
            if cache_key not in campaigns_by_account:
                table, account = _account(rule["workspace_id"], rule["account_id"])
                if not account:
                    raise HTTPException(400, "Advertising account is unavailable.")
                platform = account.get("platform") or "meta_ads"
                if platform not in PROVIDERS:
                    raise HTTPException(400, "Advertising provider is unavailable.")
                rows = _campaign_rows(platform, table, account)
                campaigns_by_account[cache_key] = {str(item.get("id") or ""): item for item in rows}
            table, account = _account(rule["workspace_id"], rule["account_id"])
            platform = account.get("platform") or "meta_ads"
            campaign = campaigns_by_account[cache_key].get(str(rule["campaign_id"]))
            if not campaign:
                raise HTTPException(404, "Campaign was not returned by the advertising provider.")
            if campaign.get("reporting_period") != "last 30 days":
                raise HTTPException(502, "Last-30-day performance data is unavailable; no campaign change was made.")
            if campaign.get("spend_amount") in (None, ""):
                raise HTTPException(502, "Last-30-day spend is unavailable; no campaign change was made.")
            spend = Decimal(str(campaign["spend_amount"]))
            conversion_limit_enabled = bool(rule.get("conversion_limit_enabled", 1))
            if conversion_limit_enabled and not campaign.get("conversions_available"):
                raise HTTPException(502, "Conversion data is unavailable for this provider/account; no campaign change was made. Disable the conversion condition to use a spend-only rule.")
            conversions = Decimal(str(campaign.get("conversions") or "0")) if campaign.get("conversions_available") else None
            current_status = str(campaign.get("status") or "").upper()
            action = "checked"
            detail = f"Last 30 days: spend {spend}; " + (f"conversions {conversions}." if conversions is not None else "conversion limit not used.")
            conversion_condition = not conversion_limit_enabled or conversions <= Decimal(str(rule["maximum_30d_conversions"]))
            if current_status in {"ACTIVE", "ENABLED", "RUNNING"} and spend >= Decimal(str(rule["minimum_30d_spend"])) and conversion_condition:
                _set_campaign_status(platform, table, account, str(rule["campaign_id"]), "PAUSED")
                action = "paused"
                detail += " Campaign paused by this guardrail."
                paused += 1
            execute(
                "UPDATE ad_optimization_rules SET last_checked_at = ?, last_action = ?, last_detail = ?, updated_at = ? WHERE id = ?",
                (now_iso(), action, detail[:500], now_iso(), rule["id"]),
            )
            checked += 1
        except (HTTPException, InvalidOperation, ValueError) as error:
            detail = str(getattr(error, "detail", "") or error)[:500]
            execute(
                "UPDATE ad_optimization_rules SET last_checked_at = ?, last_action = 'error', last_detail = ?, updated_at = ? WHERE id = ?",
                (now_iso(), detail, now_iso(), rule["id"]),
            )
            failed += 1
    return {"checked": checked, "paused": paused, "failed": failed}
