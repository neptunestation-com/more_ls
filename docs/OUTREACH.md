# Outreach email: real requirement texts (#1)

Draft email asking customers to opt in and share access-control requirements for the eval corpus
(issue #1). Fill in the parts in [brackets] before sending.

Before sending:
- The privacy paragraph is a placeholder: decide what to promise about how the cases are used,
  and check any rules your company has about customer information.
- "What you'd get back" is a suggestion; change it or drop it.
- The tool is not named and nothing is linked; add a name or link if the project is public.

---

**Subject:** Would you share a few of your access rules? (opt-in, anonymized, ~3 short paragraphs)

Hi [Name],

I'm working on a tool that writes Postgres row-level security for you. You describe who may see
and change which rows in plain English. It writes the RLS policies and constraints, and checks
them before anything is deployed. It works on the schema you already have, so you keep your
tables and it replaces the policies.

So far I've tested it only on access rules I wrote myself. That tells me little, because I
phrased the rules the same way the tool expects them. I want to test it on rules written the way
real teams write them, vague parts and all. That's why I'm writing to you.

**Would you opt in by sharing a few of your access-control requirements?** Here's exactly what
I'd need. No SQL, no policies and no code.

For each case:

1. **The rule, in a short paragraph (4–10 sentences), written the way your team or a ticket would
   write it.** Please rename the domain, tables and roles, but keep how the rule works. For
   example: *"Agents see the tickets of their organization; a customer sees only their own; a
   closed ticket can't be reopened by the customer."*
2. **A rough sketch of the tables involved.** List the tables and their main columns, and say
   which column ties a user to an organization, team or group. A plain list is fine, not a schema
   dump.
3. **One sentence on the tricky part.** That's the rule people got wrong in practice, or the one
   you couldn't express in RLS, or couldn't express efficiently.

**To start, three cases are plenty.** Put them in a text file or the body of a reply, in whatever
shape they come to mind. I'll turn them into test cases and show you the result before asking for
anything more.

**Variety helps more than volume.** Patterns I'd especially like to see:
- multi-tenant apps with roles
- ownership with delegation (someone acting on another's behalf)
- org or team hierarchies
- time windows
- soft delete
- columns some users can't see
- limits per parent (e.g. at most N members per team)

**Rules the tool can't express yet are welcome too, and are some of the most useful.** If you
suspect a rule is unusual or hard to express, send it anyway. It shows me where the tool falls
short.

**Privacy:** Please anonymize before sending; don't include real table names, customer names or
data. [I'll use what you send only to test and improve the tool, and won't publish it in a way
that can be traced back to you or your company without asking first.]

**What you'd get back:** [for each case, the policies the tool writes and its check of whether
they enforce your rule — a free second opinion on rules you may already have in production.]

If you're up for it, just reply "yes" and send cases whenever suits you; [a rough date] would be
ideal. If you'd rather talk it through first, I'm happy to get on a call.

Thanks,
[Your name]
