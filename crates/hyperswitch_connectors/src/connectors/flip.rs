pub mod transformers;

use api_models::webhooks::IncomingWebhookEvent;
#[cfg(feature = "payouts")]
use common_utils::request::{Method, RequestBuilder, RequestContent};
use common_utils::{errors::CustomResult, ext_traits::ByteSliceExt, request::Request};
#[cfg(not(feature = "payouts"))]
use error_stack::report;
use error_stack::ResultExt;
use hyperswitch_domain_models::{
    router_data::{AccessToken, ConnectorAuthType, ErrorResponse, RouterData},
    router_flow_types::{
        AccessTokenAuth, Authorize, Capture, Execute, PSync, PaymentMethodToken, RSync, Session,
        SetupMandate, Void,
    },
    router_request_types::{
        AccessTokenRequestData, PaymentMethodTokenizationData, PaymentsAuthorizeData,
        PaymentsCancelData, PaymentsCaptureData, PaymentsSessionData, PaymentsSyncData,
        RefundsData, SetupMandateRequestData,
    },
    router_response_types::{
        ConnectorInfo, PaymentsResponseData, RefundsResponseData, SupportedPaymentMethods,
    },
};
#[cfg(feature = "payouts")]
use hyperswitch_domain_models::{
    router_flow_types::{
        PoCancel, PoCreate, PoEligibility, PoFulfill, PoQuote, PoRecipient, PoSync,
    },
    types::{PayoutsData, PayoutsResponseData, PayoutsRouterData},
};
#[cfg(feature = "payouts")]
use hyperswitch_interfaces::types::{PayoutCreateType, PayoutFulfillType, PayoutSyncType};
use hyperswitch_interfaces::{
    api::{
        self, ConnectorCommon, ConnectorCommonExt, ConnectorIntegration, ConnectorSpecifications,
        Refund, RefundExecute, RefundSync,
    },
    configs::Connectors,
    errors::ConnectorError,
    events::connector_api_logs::ConnectorEvent,
    types::Response,
    webhooks::{IncomingWebhook, IncomingWebhookRequestDetails, WebhookContext},
};
#[cfg(feature = "payouts")]
use hyperswitch_masking::PeekInterface;
use hyperswitch_masking::{Mask as _, Maskable};
#[cfg(feature = "payouts")]
use router_env::{instrument, tracing};

use self::transformers as flip;
use crate::constants::headers;
#[cfg(feature = "payouts")]
use crate::types::ResponseRouterData;

#[derive(Clone)]
pub struct Flip;

impl Flip {
    pub fn new() -> &'static Self {
        &Self {}
    }
}

impl<Flow, Request, Response> ConnectorCommonExt<Flow, Request, Response> for Flip
where
    Self: ConnectorIntegration<Flow, Request, Response>,
{
    #[cfg(feature = "payouts")]
    fn build_headers(
        &self,
        req: &RouterData<Flow, Request, Response>,
        _connectors: &Connectors,
    ) -> CustomResult<Vec<(String, Maskable<String>)>, ConnectorError> {
        let auth = flip::FlipAuthType::try_from(&req.connector_auth_type)
            .change_context(ConnectorError::FailedToObtainAuthType)?;
        Ok(vec![
            (
                headers::CONTENT_TYPE.to_string(),
                "application/x-www-form-urlencoded".to_string().into(),
            ),
            (
                headers::AUTHORIZATION.to_string(),
                auth.to_authorization_header().into_masked(),
            ),
        ])
    }
}

impl ConnectorCommon for Flip {
    fn id(&self) -> &'static str {
        "flip"
    }

    fn get_auth_header(
        &self,
        auth_type: &ConnectorAuthType,
    ) -> CustomResult<Vec<(String, Maskable<String>)>, ConnectorError> {
        let auth = flip::FlipAuthType::try_from(auth_type)
            .change_context(ConnectorError::FailedToObtainAuthType)?;
        Ok(vec![(
            headers::AUTHORIZATION.to_string(),
            auth.to_authorization_header().into_masked(),
        )])
    }

    fn base_url<'a>(&self, connectors: &'a Connectors) -> &'a str {
        connectors.flip.base_url.as_ref()
    }

    fn build_error_response(
        &self,
        res: Response,
        event_builder: Option<&mut ConnectorEvent>,
    ) -> CustomResult<ErrorResponse, ConnectorError> {
        let response: flip::ErrorResponse = res
            .response
            .parse_struct("FlipErrorResponse")
            .change_context(ConnectorError::ResponseDeserializationFailed)?;

        event_builder.map(|i| i.set_response_body(&response));
        router_env::logger::info!(connector_response=?response);

        let (code, message) = response
            .errors
            .as_ref()
            .and_then(|errs| errs.first())
            .map(|e| {
                (
                    e.code.map(|c| c.to_string()).unwrap_or_default(),
                    format!("{}: {}", e.attribute.clone().unwrap_or_default(), e.message),
                )
            })
            .unwrap_or_else(|| {
                (
                    response.name.unwrap_or_default(),
                    response.message.unwrap_or_default(),
                )
            });

        Ok(ErrorResponse {
            status_code: res.status_code,
            code,
            message,
            reason: None,
            attempt_status: None,
            connector_transaction_id: None,
            connector_response_reference_id: None,
            network_advice_code: None,
            network_decline_code: None,
            network_error_message: None,
            connector_metadata: None,
        })
    }
}

impl api::Payment for Flip {}
impl api::PaymentAuthorize for Flip {}
impl api::PaymentSync for Flip {}
impl api::PaymentVoid for Flip {}
impl api::PaymentCapture for Flip {}
impl api::MandateSetup for Flip {}
impl api::ConnectorAccessToken for Flip {}
impl api::PaymentToken for Flip {}
impl api::ConnectorValidation for Flip {}

impl ConnectorIntegration<PaymentMethodToken, PaymentMethodTokenizationData, PaymentsResponseData>
    for Flip
{
}

impl ConnectorIntegration<AccessTokenAuth, AccessTokenRequestData, AccessToken> for Flip {}

impl ConnectorIntegration<SetupMandate, SetupMandateRequestData, PaymentsResponseData> for Flip {
    fn build_request(
        &self,
        _req: &RouterData<SetupMandate, SetupMandateRequestData, PaymentsResponseData>,
        _connectors: &Connectors,
    ) -> CustomResult<Option<Request>, ConnectorError> {
        Err(ConnectorError::NotImplemented("Setup Mandate flow for Flip".to_string()).into())
    }
}

impl api::PaymentSession for Flip {}

impl ConnectorIntegration<Session, PaymentsSessionData, PaymentsResponseData> for Flip {}

impl ConnectorIntegration<Capture, PaymentsCaptureData, PaymentsResponseData> for Flip {}

impl ConnectorIntegration<PSync, PaymentsSyncData, PaymentsResponseData> for Flip {}

impl ConnectorIntegration<Authorize, PaymentsAuthorizeData, PaymentsResponseData> for Flip {}

impl ConnectorIntegration<Void, PaymentsCancelData, PaymentsResponseData> for Flip {}

impl api::Payouts for Flip {}
#[cfg(feature = "payouts")]
impl api::PayoutCancel for Flip {}
#[cfg(feature = "payouts")]
impl api::PayoutCreate for Flip {}
#[cfg(feature = "payouts")]
impl api::PayoutEligibility for Flip {}
#[cfg(feature = "payouts")]
impl api::PayoutQuote for Flip {}
#[cfg(feature = "payouts")]
impl api::PayoutRecipient for Flip {}
#[cfg(feature = "payouts")]
impl api::PayoutFulfill for Flip {}
#[cfg(feature = "payouts")]
impl api::PayoutSync for Flip {}

// Flip's disbursement API executes the transfer as soon as it is created — it
// has no separate draft/confirm steps. The payout "create" step is therefore
// mapped to a bank-account inquiry (recipient validation) and the actual
// disbursement is sent during the fulfill step, so money only moves when the
// payout is confirmed.

#[cfg(feature = "payouts")]
impl ConnectorIntegration<PoCreate, PayoutsData, PayoutsResponseData> for Flip {
    fn get_url(
        &self,
        _req: &PayoutsRouterData<PoCreate>,
        connectors: &Connectors,
    ) -> CustomResult<String, ConnectorError> {
        Ok(format!(
            "{}v2/disbursement/bank-account-inquiry",
            connectors.flip.base_url
        ))
    }

    fn get_headers(
        &self,
        req: &PayoutsRouterData<PoCreate>,
        connectors: &Connectors,
    ) -> CustomResult<Vec<(String, Maskable<String>)>, ConnectorError> {
        self.build_headers(req, connectors)
    }

    fn get_request_body(
        &self,
        req: &PayoutsRouterData<PoCreate>,
        _connectors: &Connectors,
    ) -> CustomResult<RequestContent, ConnectorError> {
        let connector_req = flip::FlipBankAccountInquiryRequest::try_from(req)?;
        Ok(RequestContent::FormUrlEncoded(Box::new(connector_req)))
    }

    fn build_request(
        &self,
        req: &PayoutsRouterData<PoCreate>,
        connectors: &Connectors,
    ) -> CustomResult<Option<Request>, ConnectorError> {
        let request = RequestBuilder::new()
            .method(Method::Post)
            .url(&PayoutCreateType::get_url(self, req, connectors)?)
            .attach_default_headers()
            .headers(PayoutCreateType::get_headers(self, req, connectors)?)
            .set_body(PayoutCreateType::get_request_body(self, req, connectors)?)
            .build();

        Ok(Some(request))
    }

    #[instrument(skip_all)]
    fn handle_response(
        &self,
        data: &PayoutsRouterData<PoCreate>,
        event_builder: Option<&mut ConnectorEvent>,
        res: Response,
    ) -> CustomResult<PayoutsRouterData<PoCreate>, ConnectorError> {
        let response: flip::FlipBankAccountInquiryResponse = res
            .response
            .parse_struct("FlipBankAccountInquiryResponse")
            .change_context(ConnectorError::ResponseDeserializationFailed)?;

        event_builder.map(|i| i.set_response_body(&response));
        router_env::logger::info!(connector_response=?response);

        RouterData::try_from(ResponseRouterData {
            response,
            data: data.clone(),
            http_code: res.status_code,
        })
    }

    fn get_error_response(
        &self,
        res: Response,
        event_builder: Option<&mut ConnectorEvent>,
    ) -> CustomResult<ErrorResponse, ConnectorError> {
        self.build_error_response(res, event_builder)
    }
}

#[cfg(feature = "payouts")]
impl ConnectorIntegration<PoFulfill, PayoutsData, PayoutsResponseData> for Flip {
    fn get_url(
        &self,
        _req: &PayoutsRouterData<PoFulfill>,
        connectors: &Connectors,
    ) -> CustomResult<String, ConnectorError> {
        Ok(format!("{}v3/disbursement", connectors.flip.base_url))
    }

    fn get_headers(
        &self,
        req: &PayoutsRouterData<PoFulfill>,
        connectors: &Connectors,
    ) -> CustomResult<Vec<(String, Maskable<String>)>, ConnectorError> {
        let mut headers = self.build_headers(req, connectors)?;
        // Idempotent disbursement: retries and duplicate fulfill calls reuse the
        // same payout id so Flip deduplicates them.
        headers.push((
            "idempotency-key".to_string(),
            req.request.payout_id.get_string_repr().to_string().into(),
        ));
        Ok(headers)
    }

    fn get_request_body(
        &self,
        req: &PayoutsRouterData<PoFulfill>,
        _connectors: &Connectors,
    ) -> CustomResult<RequestContent, ConnectorError> {
        let connector_req = flip::FlipDisbursementRequest::try_from(req)?;
        Ok(RequestContent::FormUrlEncoded(Box::new(connector_req)))
    }

    fn build_request(
        &self,
        req: &PayoutsRouterData<PoFulfill>,
        connectors: &Connectors,
    ) -> CustomResult<Option<Request>, ConnectorError> {
        let request = RequestBuilder::new()
            .method(Method::Post)
            .url(&PayoutFulfillType::get_url(self, req, connectors)?)
            .attach_default_headers()
            .headers(PayoutFulfillType::get_headers(self, req, connectors)?)
            .set_body(PayoutFulfillType::get_request_body(self, req, connectors)?)
            .build();

        Ok(Some(request))
    }

    #[instrument(skip_all)]
    fn handle_response(
        &self,
        data: &PayoutsRouterData<PoFulfill>,
        event_builder: Option<&mut ConnectorEvent>,
        res: Response,
    ) -> CustomResult<PayoutsRouterData<PoFulfill>, ConnectorError> {
        let response: flip::FlipDisbursementResponse = res
            .response
            .parse_struct("FlipDisbursementResponse")
            .change_context(ConnectorError::ResponseDeserializationFailed)?;

        event_builder.map(|i| i.set_response_body(&response));
        router_env::logger::info!(connector_response=?response);

        RouterData::try_from(ResponseRouterData {
            response,
            data: data.clone(),
            http_code: res.status_code,
        })
    }

    fn get_error_response(
        &self,
        res: Response,
        event_builder: Option<&mut ConnectorEvent>,
    ) -> CustomResult<ErrorResponse, ConnectorError> {
        self.build_error_response(res, event_builder)
    }
}

#[cfg(feature = "payouts")]
impl ConnectorIntegration<PoSync, PayoutsData, PayoutsResponseData> for Flip {
    fn get_url(
        &self,
        req: &PayoutsRouterData<PoSync>,
        connectors: &Connectors,
    ) -> CustomResult<String, ConnectorError> {
        let disbursement_id = req.request.connector_payout_id.to_owned().ok_or(
            ConnectorError::MissingRequiredField {
                field_name: "connector_payout_id".into(),
            },
        )?;
        Ok(format!(
            "{}v3/disbursement/{}",
            connectors.flip.base_url, disbursement_id
        ))
    }

    fn get_headers(
        &self,
        req: &PayoutsRouterData<PoSync>,
        connectors: &Connectors,
    ) -> CustomResult<Vec<(String, Maskable<String>)>, ConnectorError> {
        self.build_headers(req, connectors)
    }

    fn build_request(
        &self,
        req: &PayoutsRouterData<PoSync>,
        connectors: &Connectors,
    ) -> CustomResult<Option<Request>, ConnectorError> {
        let request = RequestBuilder::new()
            .method(Method::Get)
            .url(&PayoutSyncType::get_url(self, req, connectors)?)
            .attach_default_headers()
            .headers(PayoutSyncType::get_headers(self, req, connectors)?)
            .build();

        Ok(Some(request))
    }

    #[instrument(skip_all)]
    fn handle_response(
        &self,
        data: &PayoutsRouterData<PoSync>,
        event_builder: Option<&mut ConnectorEvent>,
        res: Response,
    ) -> CustomResult<PayoutsRouterData<PoSync>, ConnectorError> {
        let response: flip::FlipSyncResponse = res
            .response
            .parse_struct("FlipSyncResponse")
            .change_context(ConnectorError::ResponseDeserializationFailed)?;

        event_builder.map(|i| i.set_response_body(&response));
        router_env::logger::info!(connector_response=?response);

        RouterData::try_from(ResponseRouterData {
            response,
            data: data.clone(),
            http_code: res.status_code,
        })
    }

    fn get_error_response(
        &self,
        res: Response,
        event_builder: Option<&mut ConnectorEvent>,
    ) -> CustomResult<ErrorResponse, ConnectorError> {
        self.build_error_response(res, event_builder)
    }
}

// Flip disburses immediately on creation and exposes no quote, recipient or
// cancel API — these flows are not applicable.
#[cfg(feature = "payouts")]
impl ConnectorIntegration<PoQuote, PayoutsData, PayoutsResponseData> for Flip {
    fn build_request(
        &self,
        _req: &PayoutsRouterData<PoQuote>,
        _connectors: &Connectors,
    ) -> CustomResult<Option<Request>, ConnectorError> {
        Err(ConnectorError::NotImplemented("Payout Quote for Flip".to_string()).into())
    }
}

#[cfg(feature = "payouts")]
impl ConnectorIntegration<PoRecipient, PayoutsData, PayoutsResponseData> for Flip {
    fn build_request(
        &self,
        _req: &PayoutsRouterData<PoRecipient>,
        _connectors: &Connectors,
    ) -> CustomResult<Option<Request>, ConnectorError> {
        Err(ConnectorError::NotImplemented("Payout Recipient for Flip".to_string()).into())
    }
}

#[cfg(feature = "payouts")]
impl ConnectorIntegration<PoEligibility, PayoutsData, PayoutsResponseData> for Flip {
    fn build_request(
        &self,
        _req: &PayoutsRouterData<PoEligibility>,
        _connectors: &Connectors,
    ) -> CustomResult<Option<Request>, ConnectorError> {
        Err(ConnectorError::NotImplemented("Payout Eligibility for Flip".to_string()).into())
    }
}

#[cfg(feature = "payouts")]
impl ConnectorIntegration<PoCancel, PayoutsData, PayoutsResponseData> for Flip {
    fn build_request(
        &self,
        _req: &PayoutsRouterData<PoCancel>,
        _connectors: &Connectors,
    ) -> CustomResult<Option<Request>, ConnectorError> {
        Err(ConnectorError::NotImplemented("Payout Cancel for Flip".to_string()).into())
    }
}

impl Refund for Flip {}
impl RefundExecute for Flip {}
impl RefundSync for Flip {}

impl ConnectorIntegration<Execute, RefundsData, RefundsResponseData> for Flip {}

impl ConnectorIntegration<RSync, RefundsData, RefundsResponseData> for Flip {}

#[async_trait::async_trait]
impl IncomingWebhook for Flip {
    // Flip does not sign callbacks; the disbursement callback endpoint is
    // configured in the Flip dashboard with a static token in the URL which
    // Hyperswitch already matches against the configured webhook endpoint.
    fn get_webhook_source_verification_algorithm(
        &self,
        _request: &IncomingWebhookRequestDetails<'_>,
    ) -> CustomResult<Box<dyn common_utils::crypto::VerifySignature + Send>, ConnectorError> {
        Ok(Box::new(common_utils::crypto::NoAlgorithm))
    }

    fn get_webhook_source_verification_signature(
        &self,
        #[cfg(feature = "payouts")] request: &IncomingWebhookRequestDetails<'_>,
        #[cfg(not(feature = "payouts"))] _request: &IncomingWebhookRequestDetails<'_>,
        _connector_webhook_secrets: &api_models::webhooks::ConnectorWebhookSecrets,
    ) -> CustomResult<Vec<u8>, ConnectorError> {
        #[cfg(feature = "payouts")]
        {
            // Token expected as `?token=<secret>` on the callback URL.
            Ok(request.query_params.as_bytes().to_vec())
        }
        #[cfg(not(feature = "payouts"))]
        {
            Err(report!(ConnectorError::WebhooksNotImplemented))
        }
    }

    fn get_webhook_source_verification_message(
        &self,
        #[cfg(feature = "payouts")] request: &IncomingWebhookRequestDetails<'_>,
        #[cfg(not(feature = "payouts"))] _request: &IncomingWebhookRequestDetails<'_>,
        _merchant_id: &common_utils::id_type::MerchantId,
        connector_webhook_secrets: &api_models::webhooks::ConnectorWebhookSecrets,
    ) -> CustomResult<Vec<u8>, ConnectorError> {
        #[cfg(feature = "payouts")]
        {
            let _ = request;
            connector_webhook_secrets
                .additional_secret
                .as_ref()
                .map(|s| s.peek().as_bytes().to_vec())
                .ok_or(ConnectorError::WebhookSourceVerificationFailed.into())
        }
        #[cfg(not(feature = "payouts"))]
        {
            Err(report!(ConnectorError::WebhooksNotImplemented))
        }
    }

    fn get_webhook_object_reference_id(
        &self,
        #[cfg(feature = "payouts")] request: &IncomingWebhookRequestDetails<'_>,
        #[cfg(not(feature = "payouts"))] _request: &IncomingWebhookRequestDetails<'_>,
    ) -> CustomResult<api_models::webhooks::ObjectReferenceId, ConnectorError> {
        #[cfg(feature = "payouts")]
        {
            let payload: flip::FlipPayoutsWebhookBody = request
                .body
                .parse_struct("FlipPayoutsWebhookBody")
                .change_context(ConnectorError::WebhookReferenceIdNotFound)?;

            Ok(api_models::webhooks::ObjectReferenceId::PayoutId(
                api_models::webhooks::PayoutIdType::ConnectorPayoutId(payload.id.to_string()),
            ))
        }
        #[cfg(not(feature = "payouts"))]
        {
            Err(report!(ConnectorError::WebhooksNotImplemented))
        }
    }

    fn get_webhook_event_type(
        &self,
        #[cfg(feature = "payouts")] request: &IncomingWebhookRequestDetails<'_>,
        #[cfg(not(feature = "payouts"))] _request: &IncomingWebhookRequestDetails<'_>,
        _context: Option<&WebhookContext>,
    ) -> CustomResult<IncomingWebhookEvent, ConnectorError> {
        #[cfg(feature = "payouts")]
        {
            let payload: flip::FlipPayoutsWebhookBody = request
                .body
                .parse_struct("FlipPayoutsWebhookBody")
                .change_context(ConnectorError::WebhookReferenceIdNotFound)?;

            Ok(flip::get_flip_webhooks_event(&payload.status))
        }
        #[cfg(not(feature = "payouts"))]
        {
            Err(report!(ConnectorError::WebhooksNotImplemented))
        }
    }

    fn get_webhook_resource_object(
        &self,
        #[cfg(feature = "payouts")] request: &IncomingWebhookRequestDetails<'_>,
        #[cfg(not(feature = "payouts"))] _request: &IncomingWebhookRequestDetails<'_>,
    ) -> CustomResult<Box<dyn hyperswitch_masking::ErasedMaskSerialize>, ConnectorError> {
        #[cfg(feature = "payouts")]
        {
            let payload: flip::FlipPayoutsWebhookBody = request
                .body
                .parse_struct("FlipPayoutsWebhookBody")
                .change_context(ConnectorError::WebhookReferenceIdNotFound)?;

            Ok(Box::new(payload))
        }
        #[cfg(not(feature = "payouts"))]
        {
            Err(report!(ConnectorError::WebhooksNotImplemented))
        }
    }
}

static FLIP_CONNECTOR_INFO: ConnectorInfo = ConnectorInfo {
    display_name: "Flip",
    description: "Flip for Business disbursement connector enabling payouts to Indonesian bank accounts and e-wallets through the Flip Money Transfer API.",
    connector_type: common_enums::HyperswitchConnectorCategory::PayoutProcessor,
    integration_status: common_enums::ConnectorIntegrationStatus::Sandbox,
};

impl ConnectorSpecifications for Flip {
    fn get_connector_about(&self) -> Option<&'static ConnectorInfo> {
        Some(&FLIP_CONNECTOR_INFO)
    }

    fn get_supported_payment_methods(&self) -> Option<&'static SupportedPaymentMethods> {
        None
    }

    fn get_supported_webhook_flows(&self) -> Option<&'static [common_enums::enums::EventClass]> {
        Some(&[common_enums::enums::EventClass::Payouts])
    }
}
