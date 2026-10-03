#[cfg(feature = "payouts")]
use std::fmt;

#[cfg(feature = "payouts")]
use api_models::payouts::{BankTransfer, PayoutMethodData};
#[cfg(feature = "payouts")]
use common_enums::{PayoutStatus, PayoutType};
#[cfg(feature = "payouts")]
use common_utils::pii::Email;
use hyperswitch_domain_models::router_data::ConnectorAuthType;
#[cfg(feature = "payouts")]
use hyperswitch_domain_models::types::{PayoutsResponseData, PayoutsRouterData};
use hyperswitch_interfaces::errors::ConnectorError;
use hyperswitch_masking::{PeekInterface, Secret};
use serde::{Deserialize, Serialize};

#[cfg(feature = "payouts")]
use crate::types::PayoutsResponseRouterData;
#[cfg(feature = "payouts")]
use crate::utils::{
    get_unimplemented_payment_method_error_message, PayoutsData as _, RouterData as _,
};

type Error = error_stack::Report<ConnectorError>;

// Flip for Business uses HTTP Basic auth with the secret key as username and a
// blank password: Authorization = Basic base64("<secret_key>:")
pub struct FlipAuthType {
    pub(super) api_key: Secret<String>,
}

impl FlipAuthType {
    pub fn to_authorization_header(&self) -> Secret<String> {
        use base64::Engine;
        let encoded =
            base64::engine::general_purpose::STANDARD.encode(format!("{}:", self.api_key.peek()));
        Secret::new(format!("Basic {encoded}"))
    }
}

impl TryFrom<&ConnectorAuthType> for FlipAuthType {
    type Error = Error;
    fn try_from(auth_type: &ConnectorAuthType) -> Result<Self, Self::Error> {
        match auth_type {
            ConnectorAuthType::HeaderKey { api_key } => Ok(Self {
                api_key: api_key.to_owned(),
            }),
            ConnectorAuthType::BodyKey { api_key, .. } => Ok(Self {
                api_key: api_key.to_owned(),
            }),
            _ => Err(ConnectorError::FailedToObtainAuthType)?,
        }
    }
}

// Flip error response shape:
// {"name": "...", "message": "...", "status": "...", "errors": [{"attribute": ..., "code": ..., "message": ...}]}
#[derive(Debug, Default, Deserialize, Serialize)]
pub struct ErrorResponse {
    pub name: Option<String>,
    pub message: Option<String>,
    pub status: Option<String>,
    pub errors: Option<Vec<FlipErrorDetail>>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct FlipErrorDetail {
    pub attribute: Option<String>,
    pub code: Option<i32>,
    pub message: String,
}

// Flip transaction ids are 19-digit BigInteger values. They must be handled as
// strings or 64-bit-safe values; deserialize leniently to support both
// JSON number and JSON string encodings.
#[cfg(feature = "payouts")]
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(untagged)]
pub enum FlipId {
    Number(u64),
    Str(String),
}

#[cfg(feature = "payouts")]
impl fmt::Display for FlipId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Number(value) => write!(f, "{value}"),
            Self::Str(value) => write!(f, "{value}"),
        }
    }
}

#[cfg(feature = "payouts")]
#[derive(Debug, Default, Clone, Deserialize, Serialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum FlipStatus {
    #[default]
    Pending,
    Done,
    Cancelled,
    Processing,
    Rejected,
    Inactive,
    #[serde(other)]
    Unknown,
}

#[cfg(feature = "payouts")]
impl From<FlipStatus> for PayoutStatus {
    fn from(status: FlipStatus) -> Self {
        match status {
            // The disbursement was accepted and is being processed by Flip
            FlipStatus::Pending | FlipStatus::Processing | FlipStatus::Unknown => Self::Initiated,
            FlipStatus::Done => Self::Success,
            FlipStatus::Cancelled => Self::Cancelled,
            FlipStatus::Rejected | FlipStatus::Inactive => Self::Failed,
        }
    }
}

// ---------------------------------------------------------------------------
// Bank account inquiry — POST /v2/disbursement/bank-account-inquiry
// Used in the payout create step to validate the recipient account before any
// money moves. Fulfillment performs the actual disbursement.
// ---------------------------------------------------------------------------
#[cfg(feature = "payouts")]
#[derive(Debug, Serialize)]
pub struct FlipBankAccountInquiryRequest {
    pub account_number: Secret<String>,
    pub bank_code: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub inquiry_key: Option<String>,
}

#[allow(dead_code)]
#[cfg(feature = "payouts")]
#[derive(Debug, Deserialize, Serialize)]
pub struct FlipBankAccountInquiryResponse {
    pub bank_code: Option<String>,
    pub account_number: Option<Secret<String>>,
    pub account_holder: Option<Secret<String>>,
    pub status: Option<String>,
    pub inquiry_key: Option<String>,
}

// ---------------------------------------------------------------------------
// Disbursement — POST /v3/disbursement  (executes the transfer immediately)
// ---------------------------------------------------------------------------
#[cfg(feature = "payouts")]
#[derive(Debug, Serialize)]
pub struct FlipDisbursementRequest {
    pub account_number: Secret<String>,
    pub bank_code: String,
    pub amount: String,
    pub remark: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub beneficiary_email: Option<Email>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub beneficiary_phone: Option<Secret<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub recipient_city: Option<String>,
}

#[allow(dead_code)]
#[cfg(feature = "payouts")]
#[derive(Debug, Deserialize, Serialize)]
pub struct FlipDisbursementResponse {
    pub id: FlipId,
    #[serde(default)]
    pub status: FlipStatus,
    pub account_number: Option<Secret<String>>,
    pub bank_code: Option<String>,
    pub recipient_name: Option<Secret<String>>,
    pub recipient_city: Option<String>,
    pub amount: Option<u64>,
    pub fee: Option<u64>,
    pub total_amount: Option<u64>,
    pub remark: Option<String>,
    pub beneficiary_email: Option<String>,
    pub idempotency_key: Option<String>,
    pub is_instant: Option<bool>,
    pub timestamp: Option<String>,
    pub time_served: Option<String>,
}

// ---------------------------------------------------------------------------
// Get disbursement — GET /v3/disbursement/{id}
// Same payload shape as create
// ---------------------------------------------------------------------------
#[cfg(feature = "payouts")]
pub type FlipSyncResponse = FlipDisbursementResponse;

// Indonesian local bank transfer rides on the generic `bank_transfer` payout
// method using the ACH variant as the wire carrier:
//   - ach.bank_routing_number -> Flip `bank_code` (e.g. "bca", "bni", "bri",
//     "mandiri", "cimb", "gopay", "dana", "ovo", ...)
//   - ach.bank_account_number -> Flip `account_number`
//   - ach.bank_city           -> Flip `recipient_city` (optional)
//   - ach.account_holder_name -> recipient name (validated server-side by Flip)
#[cfg(feature = "payouts")]
fn get_flip_bank_details<F>(
    item: &PayoutsRouterData<F>,
) -> Result<(Secret<String>, String, Option<String>), Error> {
    match item.get_payout_method_data()? {
        PayoutMethodData::BankTransfer(BankTransfer::Ach(bank)) => Ok((
            bank.bank_account_number,
            bank.bank_routing_number.peek().to_string(),
            bank.bank_city,
        )),
        _ => Err(
            ConnectorError::NotImplemented(get_unimplemented_payment_method_error_message("Flip"))
                .into(),
        ),
    }
}

#[cfg(feature = "payouts")]
impl<F> TryFrom<&PayoutsRouterData<F>> for FlipBankAccountInquiryRequest {
    type Error = Error;
    fn try_from(item: &PayoutsRouterData<F>) -> Result<Self, Self::Error> {
        let request = &item.request;
        match request.get_payout_type()? {
            PayoutType::Bank => {
                let (account_number, bank_code, _) = get_flip_bank_details(item)?;
                Ok(Self {
                    account_number,
                    bank_code,
                    inquiry_key: Some(request.payout_id.get_string_repr().to_string()),
                })
            }
            PayoutType::Card | PayoutType::Wallet | PayoutType::BankRedirect => Err(
                ConnectorError::NotImplemented(get_unimplemented_payment_method_error_message(
                    "Flip",
                ))
                .into(),
            ),
        }
    }
}

#[cfg(feature = "payouts")]
impl<F> TryFrom<&PayoutsRouterData<F>> for FlipDisbursementRequest {
    type Error = Error;
    fn try_from(item: &PayoutsRouterData<F>) -> Result<Self, Self::Error> {
        let request = item.request.clone();
        match request.get_payout_type()? {
            PayoutType::Bank => {
                let (account_number, bank_code, recipient_city) = get_flip_bank_details(item)?;
                let customer = request.customer_details;
                Ok(Self {
                    account_number,
                    bank_code,
                    amount: request.minor_amount.get_amount_as_i64().to_string(),
                    remark: format!("payout {}", request.payout_id.get_string_repr()),
                    beneficiary_email: customer.as_ref().and_then(|c| c.email.clone()),
                    beneficiary_phone: customer
                        .as_ref()
                        .and_then(|c| c.phone.as_ref())
                        .map(|phone| Secret::new(phone.peek().to_string())),
                    recipient_city,
                })
            }
            PayoutType::Card | PayoutType::Wallet | PayoutType::BankRedirect => Err(
                ConnectorError::NotImplemented(get_unimplemented_payment_method_error_message(
                    "Flip",
                ))
                .into(),
            ),
        }
    }
}

#[cfg(feature = "payouts")]
impl<F> TryFrom<PayoutsResponseRouterData<F, FlipDisbursementResponse>> for PayoutsRouterData<F> {
    type Error = Error;
    fn try_from(
        item: PayoutsResponseRouterData<F, FlipDisbursementResponse>,
    ) -> Result<Self, Self::Error> {
        let response = item.response;
        Ok(Self {
            response: Ok(PayoutsResponseData {
                status: Some(PayoutStatus::from(response.status)),
                connector_payout_id: Some(response.id.to_string()),
                payout_eligible: None,
                should_add_next_step_to_process_tracker: false,
                error_code: None,
                error_message: None,
                payout_connector_metadata: None,
                connector_eligibility_reference_id: None,
            }),
            ..item.data
        })
    }
}

#[cfg(feature = "payouts")]
impl<F> TryFrom<PayoutsResponseRouterData<F, FlipBankAccountInquiryResponse>>
    for PayoutsRouterData<F>
{
    type Error = Error;
    fn try_from(
        item: PayoutsResponseRouterData<F, FlipBankAccountInquiryResponse>,
    ) -> Result<Self, Self::Error> {
        let response = item.response;
        let status = match response.status.as_deref() {
            Some("SUCCESS") | Some("success") | None => PayoutStatus::RequiresFulfillment,
            Some("INVALID_ACCOUNT_NUMBER") | Some("invalid_account_number") => PayoutStatus::Failed,
            _ => PayoutStatus::Failed,
        };
        Ok(Self {
            response: Ok(PayoutsResponseData {
                status: Some(status),
                connector_payout_id: response
                    .inquiry_key
                    .or_else(|| Some(item.data.request.payout_id.get_string_repr().to_string())),
                payout_eligible: None,
                should_add_next_step_to_process_tracker: false,
                error_code: None,
                error_message: None,
                payout_connector_metadata: None,
                connector_eligibility_reference_id: None,
            }),
            ..item.data
        })
    }
}

// ---------------------------------------------------------------------------
// Flip callback (disbursement status changes to DONE / CANCELLED)
// ---------------------------------------------------------------------------
#[allow(dead_code)]
#[cfg(feature = "payouts")]
#[derive(Debug, Deserialize, Serialize)]
pub struct FlipPayoutsWebhookBody {
    pub id: FlipId,
    pub status: FlipStatus,
    pub amount: Option<u64>,
    pub bank_code: Option<String>,
    pub account_number: Option<Secret<String>>,
    pub recipient_name: Option<Secret<String>>,
    pub idempotency_key: Option<String>,
    pub timestamp: Option<String>,
    pub time_served: Option<String>,
}

#[cfg(feature = "payouts")]
pub fn get_flip_webhooks_event(status: &FlipStatus) -> api_models::webhooks::IncomingWebhookEvent {
    match status {
        FlipStatus::Done => api_models::webhooks::IncomingWebhookEvent::PayoutSuccess,
        FlipStatus::Cancelled => api_models::webhooks::IncomingWebhookEvent::PayoutCancelled,
        FlipStatus::Rejected | FlipStatus::Inactive => {
            api_models::webhooks::IncomingWebhookEvent::PayoutFailure
        }
        FlipStatus::Pending | FlipStatus::Processing | FlipStatus::Unknown => {
            api_models::webhooks::IncomingWebhookEvent::PayoutProcessing
        }
    }
}
