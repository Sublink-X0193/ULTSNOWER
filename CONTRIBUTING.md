# Contributing

感谢你愿意参与 ULTSNOWER。

## 本地开发

```powershell
python -m pip install -e ".[test]"
python -m pytest -q
```

## 提交前检查

- 不提交 `.env`、数据库、日志、真实凭据或生产配置。
- 新增行为尽量补测试。
- 对外接口变更请同步更新 `README.md` 和 `docs/`。

## Pull Request

请在 PR 中说明：

1. 改动目的。
2. 主要行为变化。
3. 已运行的测试命令。
