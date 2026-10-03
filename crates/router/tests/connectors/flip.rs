use hyperswitch_domain_models::address::{Address, AddressDetails};
use hyperswitch_masking::Secret;
use router::{
    types,
    types::{api, storage::enums, PaymentAddress},
};

use crate::{
    connector_auth,
    utils::{self, ConnectorActions, PaymentInfo},
};

struct FlipTest;

impl ConnectorActions for FlipTest {}

impl utils::Connector for FlipTest {
    fn get_data(&self) -> api::ConnectorData {
        use router::connector::Fiserv;
        utils::construct_connector_data_old(
            Box::new(Fiserv::new()),
            types::Connector::Fiserv,
            api::GetToken::Connector,
            None,
        )
    }

    fn get_payout_data(&self) -> Option<api::ConnectorData> {
        use router::connector::Flip;
        Some(utils::construct_connector_data_old(
            Box::new(Flip::new()),
            types::Connector::Flip,
            api::GetToken::Connector,
            None,
        ))
    }

    fn get_auth_token(&self) -> types::ConnectorAuthType {
        utils::to_connector_auth_type(
            connector_auth::ConnectorAuthentication::new()
                .flip
                .expect("Missing connector authentication configuration")
                .into(),
        )
    }

    fn get_name(&self) -> String {
        "flip".to_string()
    }
}

impl FlipTest {
    fn get_payout_info() -> Option<PaymentInfo> {
        Some(PaymentInfo {
            currency: Some(enums::Currency::IDR),
            address: Some(PaymentAddress::new(
                None,
                Some(Address {
                    address: Some(AddressDetails {
                        country: Some(api_models::enums::CountryAlpha2::ID),
                        city: Some("Jakarta".to_string()),
                        ..Default::default()
                    }),
                    phone: None,
                    email: None,
                }),
                None,
                None,
            )),
            payout_method_data: Some(api::PayoutMethodData::BankTransfer(
                // Indonesian local bank transfer rides the `ach` wire shape:
                // `bank_routing_number` carries the Flip `bank_code`.
                api::payouts::BankTransferPayout::Ach(api::AchBankTransfer {
                    bank_account_number: "1234567890".to_string().into(),
                    bank_routing_number: "bca".to_string().into(),
                    bank_name: Some("BCA".to_string()),
                    bank_country_code: Some(enums::CountryAlpha2::ID),
                    bank_city: Some("Jakarta".to_string()),
                    account_holder_name: Some(Secret::new("John Doe".to_string())),
                }),
            )),
            ..Default::default()
        })
    }
}

static CONNECTOR: FlipTest = FlipTest {};

/******************** Payouts test cases ********************/
// Validates the recipient account (bank-account inquiry at create step)

#[actix_web::test]
async fn should_create_payout() {
    let payout_type = enums::PayoutType::Bank;
    let payout_info = FlipTest::get_payout_info();
    let create_res: types::PayoutsResponseData = CONNECTOR
        .create_payout(None, payout_type, payout_info)
        .await
        .expect("Payout create response");
    assert_eq!(
        create_res.status.unwrap(),
        enums::PayoutStatus::RequiresFulfillment
    );
}

// Create and fulfill (disburse) payout

#[actix_web::test]
async fn should_create_and_fulfill_payout() {
    let payout_type = enums::PayoutType::Bank;
    let payout_info = FlipTest::get_payout_info();
    let response = CONNECTOR
        .create_and_fulfill_payout(None, payout_type, payout_info)
        .await
        .expect("Payout create and fulfill response");
    let status = response.status.unwrap();
    assert!(matches!(
        status,
        enums::PayoutStatus::Initiated
            | enums::PayoutStatus::Pending
            | enums::PayoutStatus::Success
    ));
}
