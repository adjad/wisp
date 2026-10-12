import asyncio
from unittest.mock import patch,Mock,AsyncMock
from tests.test_intel_remote_policy import RemoteFixture,policy,EndpointConfigurationError,config
from service.config import endpoints
from service.inference import super_model
from service.inference.readiness import TurnInferenceClient
from service.inference.inference_errors import ModelLoadError

class NoLocalChecks(RemoteFixture):
    def test_all_roles_and_settings_cannot_choose_local_or_cloud(self):
        for role in policy.remote_only_profile().generation_roles:
            self.assertEqual(config.role_to_model(role),'qwen3.6:35b');self.assertEqual(endpoints.role_target(role),policy.remote_default_target(role))
        for role in ['embedding','reranker','unknown']:
            with self.assertRaises(EndpointConfigurationError):endpoints.role_target(role)
        with patch.object(config,'_save_overlay') as write:
            for call in [lambda:config.set_roles({'fast':'other'}),lambda:config.set_local_provider({},'other',8000,['agent']),lambda:config.set_cloud_provider({},'other',8000,['reasoning']),lambda:endpoints.local_role_target('fast'),lambda:endpoints.cloud_super_model_target('reasoning')]:
                with self.assertRaises(EndpointConfigurationError):call()
            write.assert_not_called()
        self.assertFalse(config.cloud_super_model_enabled())
    def test_laya_before_import_load_or_predict(self):
        with patch.object(super_model,'_agent',None),patch.object(super_model.asyncio,'create_task') as spawn:
            for call in [super_model._load_agent,lambda:super_model._predict_with_laya('synthetic')]:
                with self.assertRaises(EndpointConfigurationError):call()
            super_model.start_laya_warmup();spawn.assert_not_called()
    def test_remote_readiness_failure_has_no_local_fallback(self):
        fake=Mock();fake.target=policy.remote_default_target();fake.managed=False
        start=AsyncMock(side_effect=ModelLoadError('synthetic unavailable'));fallback=AsyncMock(side_effect=AssertionError('local fallback forbidden'))
        turn=TurnInferenceClient(fake,start,fallback_start=fallback)
        async def run():
            with patch('service.inference.readiness.OMLXClient') as constructor:
                with self.assertRaises(ModelLoadError):await turn._ensure_generation('qwen3.6:35b')
                constructor.assert_not_called()
        asyncio.run(run());fallback.assert_not_called()
    def test_legacy_config_roles_behavior_preserved(self):
        self.legacy();self.cfg={'roles':{'fast':'legacy','agent':'legacy'},'default_role':'agent'}
        self.assertEqual(config.role_to_model('fast'),'legacy')
