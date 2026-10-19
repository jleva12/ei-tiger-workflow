"""Each organization's own agent permissions: who may use its agents.

The site's roles and permissions (forge_admin.auth) decide what people see
and do in Forge. These are apart from them: an organization defines agent
permissions and links each to the company's groups (whoever's sign-in names
one holds it) and to its own API keys. There are no agent roles: the
directory's groups say who's who. They count only in that organization; its
chat agents and workflows name the ones that let someone use them
(forge_admin.chat_agents.access, asking ``policy.holds_any``).
"""
