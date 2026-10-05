"""Synthetic gold authored before inference. No personal export contents."""
import json, hashlib
from pathlib import Path
OUT = Path(__file__).parent
cases=[]
def add(family, prompts, tools, args=None, domains=None, *, split='test', context=None, forbidden=None, absent=None, note=''):
    for i,p in enumerate(prompts):
        cases.append(dict(id=f'{family}-{i+1}',family=family,split=split,prompt=p,context=context or [],gold=dict(tools=tools,args=args or {},domains=domains or [],forbidden=forbidden or [],absent=absent or {},note=note)))
# Development families: used for harness debugging only; never count as held-out.
add('dev_agenda', ['Show my calendar today','What meetings do I have today?','Check today\'s agenda'], ['get_upcoming'], {'get_upcoming':{'period':'today'}}, ['calendar'], split='dev')
add('dev_mail', ['Summarize my email','Catch me up on my inbox','Give me an email overview'], ['summarize_emails'], domains=['email'], split='dev', absent={'summarize_emails':['period','day']})
add('dev_text', ['Summarize messages from yesterday','Catch me up on yesterday\'s texts','Give me a digest of yesterday\'s messages'], ['summarize_messages'], {'summarize_messages':{'period':'yesterday'}}, ['messages'], split='dev')
add('dev_weather', ['Weather in Seattle tomorrow?','Check tomorrow\'s Seattle weather','What is the forecast for Seattle tomorrow?'], ['get_weather'], {'get_weather':{'location':{'contains':'Seattle'},'period':'tomorrow'}}, ['weather'], split='dev')
add('dev_none', ['Thanks!','Tell me a joke','What does the word calendar mean?'], [], domains=['none'], split='dev')
add('dev_timer', ['Set a 12 minute timer','Start a timer for 12 minutes','Time twelve minutes for me'], ['set_timer'], {'set_timer':{'duration':{'duration_seconds':720}}}, ['device'], split='dev')
# Weekly paraphrases and context, the reported failure category.
add('week_casual', ['what is up for this week','what\'s up this week','what have I got going on this week','what am I doing this week','what does my week look like','show me my week','Anything on the agenda this week?','Run through this week for me','What is coming up for me this week?','What\'s happening this week?'], ['get_upcoming'], {'get_upcoming':{'period':'this week'}}, ['calendar'])
add('week_explicit', ['Show my calendar for this week','What\'s on my calender this week?','Can you give me this week\'s calendar overview?','List this week\'s meetings','Look up my appointments for this week'], ['get_upcoming'], {'get_upcoming':{'period':'this week','calendar_only':True}}, ['calendar'])
add('week_context', ['and this week?','Just this week please','What about the week instead?','Only show the current week'], ['get_upcoming'], {'get_upcoming':{'period':'this week'}}, ['calendar'], context=[{'role':'user','content':'Show me my calendar this month'},{'role':'assistant','content':'October has five appointments. [Tools: get_upcoming]'}])
add('nextweek_context', ['and next week?','How about the following week?','Next week instead, please'], ['get_upcoming'], {'get_upcoming':{'period':'next week'}}, ['calendar'], context=[{'role':'user','content':'What is on my calendar this week?'},{'role':'assistant','content':'You have two meetings this week. [Tools: get_upcoming]'}])
for fam,period,phrases in [
 ('tomorrow','tomorrow',["What is up for tmrow?","What does tomorrow look like for me?","Do I have any meetings tomorrow?","Show tomorrow's calendar"]),
 ('month','this month',["What is on my calender for this month?","Show me this month's agenda","Calendar overview for October 2026","What appointments do I have this month?"]),
 ('nextmonth','next month',["What's on my calendar next month?","What have I scheduled in November 2026?","Give me next month's calendar"]),
 ('dated','2026-10-09',["What's on my calendar on October 9, 2026?","Show my meetings for 2026-10-09","Check my calendar this Friday"]),
 ('weekend','this weekend',["What's on my calendar this weekend?","Show this weekend's appointments","Am I busy this weekend?"])]:
    add(fam,phrases,['get_upcoming'],{'get_upcoming':{'period':period}},['calendar'])
add('rolling_week',['Show my calendar for the next 7 days','What is coming up over the next seven days?','My schedule over the next 7 days please'],['get_upcoming'],{'get_upcoming':{'$range':'next 7 days'}},['calendar'])
add('calendar_only',['Show this week\'s calendar, without reminders','Just my appointments this week, no to-dos','This week\'s calendar events only please'],['get_upcoming'],{'get_upcoming':{'period':'this week','calendar_only':True}},['calendar'])
add('calendar_query',['Find dentist appointments in my calendar this month','Any dentist visits on this month\'s calendar?','Show only my dentist events for October 2026'],['get_upcoming'],{'get_upcoming':{'period':'this month','query':{'contains':'dentist'}}},['calendar'])
add('availability',['Find an hour free tomorrow','When can I fit a 60 minute meeting tomorrow?','Show 1-hour openings on October 6, 2026'],['find_free_time'],{'find_free_time':{'period':'tomorrow','minutes':60}},['calendar'])
# Read vs overview: raw records requested explicitly must stay raw.
add('mail_records',['Show me my last 5 emails','Read the five most recent emails','List my latest five emails in full'],['view_emails'],{'view_emails':{'count':5}},['email'],absent={'view_emails':['period','day']})
add('mail_overview',['Give me an overview of email from last week','Summarize last week\'s inbox','Catch me up on last week\'s emails'],['summarize_emails'],{'summarize_emails':{'period':'last week'}},['email'])
add('mail_unread',['Summarize my unread email','What haven\'t I read in my inbox?','Give me a digest of unread emails'],['summarize_emails'],{'summarize_emails':{'unread':True}},['email'],absent={'summarize_emails':['period','day']})
add('mail_lookup',['Find unread emails from Acme','Do I have any unread mail mentioning Acme?','Show only unread Acme emails'],['view_emails'],{'view_emails':{'unread':True,'query':{'contains':'Acme'}}},['email'],absent={'view_emails':['period','day']})
add('mail_specific',['Find emails about the kitchen remodel','Search my mail for the kitchen remodel','Show mail mentioning kitchen remodel'],['view_emails'],{'view_emails':{'query':{'contains':'kitchen remodel'}}},['email'])
add('mail_range',['Show emails from the last 3 weeks','Read my emails over the last twenty-one days','Find mail from the last 21 days'],['view_emails'],{'view_emails':{'period':'last 21 days'}},['email'])
add('texts_overview',['Summarize my messages','Catch me up on texts','Give me a message overview','What have I missed in my messages?'],['summarize_messages'],domains=['messages'],absent={'summarize_messages':['period','day']})
add('texts_context',['and messages?','What about texts?','Same for my messages please'],['summarize_messages'],domains=['messages'],context=[{'role':'user','content':'Give me a quick email overview'},{'role':'assistant','content':'Your inbox has two follow-ups. [Tools: summarize_emails]'}],absent={'summarize_messages':['period','day']})
add('texts_conversation',['Summarize my chat with Morgan from last week','Catch me up on last week\'s messages with Morgan','Give me a digest of the Morgan conversation for last week'],['summarize_messages'],{'summarize_messages':{'conversation':{'contains':'Morgan'},'period':'last week'}},['messages'])
add('texts_records',['Show my last 10 messages','List the ten most recent texts','Read my latest ten messages'],['view_messages'],{'view_messages':{'count':10}},['messages'])
add('texts_lookup',['Find messages about the flight booking','Search texts for flight booking','Show texts mentioning flight booking'],['view_messages'],{'view_messages':{'query':{'contains':'flight booking'}}},['messages'])
add('dual_overview',['Summarize emails and messages from yesterday','Catch me up on yesterday\'s mail and texts','Give a digest of both yesterday\'s email and iMessages'],['summarize_emails','summarize_messages'],{'summarize_emails':{'period':'yesterday'},'summarize_messages':{'period':'yesterday'}},['email','messages'])
add('exclude_email',['Catch me up on messages only; don\'t check email','Summarize texts, excluding my inbox','Give me a messages overview without reading mail'],['summarize_messages'],domains=['messages'],forbidden=['view_emails','summarize_emails'])
add('exclude_messages',['Summarize email only, leave my messages alone','Give me an inbox digest, not texts','Catch me up on email without reading iMessage'],['summarize_emails'],domains=['email'],forbidden=['view_messages','summarize_messages'])
add('notes_lookup',['Find my note about the kitchen remodel','Search Notes for kitchen remodel','Look up the kitchen remodel note'],['search_notes'],{'search_notes':{'query':{'contains':'kitchen remodel'}}},['notes'])
# Public vs personal and local-only constraints.
add('public_week',['What is happening in San Francisco this week?','Find public events in San Francisco this week','What\'s going on in San Francisco this week?','Look up this week\'s events in San Francisco'],['web_search'],{'web_search':{'query':{'contains':'San Francisco'}}},['web'])
add('public_news',['Search the web for this week\'s AI news','Find current AI news for this week','Look up recent AI headlines'],['web_search'],{'web_search':{'query':{'contains':'AI'}}},['web'])
add('local_no_web',['Show my calendar this week, don\'t search the web','What is up for this week? Only use my local calendar','Use my calendar only to tell me what\'s on this week'],['get_upcoming'],{'get_upcoming':{'period':'this week'}},['calendar'],forbidden=['web_search','web_fetch','http_request','run_shell'])
add('weather_range',['Weather in Boston this weekend?','Check the forecast for Boston this weekend','Will it rain in Boston this weekend?'],['get_weather'],{'get_weather':{'location':{'contains':'Boston'},'period':'this weekend'}},['weather'])
# Effect decisions are graded as proposed plans; no native/outbound tool is ever invoked.
add('send_literal',['Text +12025550123 that I will be 10 minutes late','Send an iMessage to +12025550123 saying I will be 10 minutes late','Message +12025550123: I will be 10 minutes late'],['send_message'],{'send_message':{'to':'+12025550123','text':{'contains':'10 minutes late'}}},['messages'])
add('mail_literal',['Email pat@example.invalid with subject Lunch and body See you at noon','Send pat@example.invalid an email titled Lunch saying See you at noon','Send an email to pat@example.invalid: subject Lunch; body See you at noon'],['send_email'],{'send_email':{'to':'pat@example.invalid','subject':'Lunch','body':'See you at noon'}},['email'])
add('draft_only',['Write a text saying I will be late; don\'t send it','Help draft an email about rescheduling lunch; show it here only','Suggest a reply saying thank you, but send nothing'],[],domains=['none'],forbidden=['send_message','send_email','draft_email','draft_message','lookup_contact'],note='Inline authored drafts need no tool or contact lookup.')
add('quoted_instruction',['Explain the phrase "send an email"','What does "cancel my calendar event" mean?','Translate "delete all my messages" into French'],[],domains=['none'],forbidden=['send_email','send_message','cancel_event','delete_email','view_messages'])
add('negated_effect',['Do not send any messages','Don\'t cancel my meetings','Never email anyone on my behalf'],[],domains=['none'],forbidden=['send_message','send_email','cancel_event'])
add('create_event',['Schedule lunch on October 9, 2026 at noon for 30 minutes','Add a 30-minute Lunch event for this Friday at 12 PM','Put lunch on my calendar 2026-10-09 at 12:00 for half an hour'],['add_calendar_event'],{'add_calendar_event':{'title':{'contains':'lunch'},'when_iso':{'iso':'2026-10-09T12:00'},'duration_min':30}},['calendar'])
add('reminder',['Remind me to call the dentist tomorrow at 4 PM','Set a reminder to call the dentist on October 6, 2026 at 16:00','Tomorrow at 4pm remind me to call the dentist'],['add_reminder'],{'add_reminder':{'title':{'contains':'dentist'},'when_iso':{'iso':'2026-10-06T16:00'}}},['calendar'])
add('battery',['How much battery is left?','Check my Mac\'s battery level','What percentage is my battery at?'],['get_battery_status'],domains=['device'])
add('files',['What\'s in my Downloads folder?','List the files in ~/Downloads','Show the contents of Downloads'],['list_dir'],{'list_dir':{'path':{'endswith':'/Downloads'}}},['files'])
add('smalltalk',['Good morning','How are you doing?','Tell me a short joke','Explain how calendars work','What does unread mean?'],[],domains=['none'])
# Ambiguous requests receive descriptive-only measurement, excluded from strict accuracy.
add('ambiguous',['What\'s new?','Any updates?','Tell Alex about it','And next week?','Anything important?'],[],domains=['unclear'],split='challenge',note='No single strict gold; inspect clarification and unsafe guesses separately.')
# Independent pre-inference gold review: runtime contracts and ambiguous cases.
for c in cases:
    if c['family']=='week_explicit': c['gold']['args']['get_upcoming'].pop('calendar_only',None)
    if c['family']=='weekend': c['gold']['args']['get_upcoming']['period']='2026-10-10 to 2026-10-11'
    if c['family']=='weather_range':
        c['split']='challenge';c['gold']['note']='Weekend is beyond three-day weather coverage; no strict routing success implies answerable forecast.'
    if c['id'] in ('local_no_web-2','local_no_web-3'):c['gold']['args']['get_upcoming']['calendar_only']=True
    if c['id'] in ('week_casual-1','week_casual-2','week_casual-8','week_casual-10'):
        c['split']='challenge';c['gold']['note']='Calendar is preferred product default; clarification is also reasonable without context.'
    if c['id']=='mail_records-1':c['prompt']='Show the full text of my last 5 emails'
    if c['id']=='mail_range-1':c['prompt']='Show emails from the last 3 weeks in full'
    if c['id']=='mail_range-3':c['prompt']='Find mail from the last 21 days and show it in full'
    if c['id']=='ambiguous-1':c['gold']['note']='get_recent_activity is a plausible supported route; this challenge is descriptive only.'
add('reported_week_context', ['what is up for this week',"what's up this week",'Run through this week for me'], ['get_upcoming'], {'get_upcoming':{'period':'this week'}}, ['calendar'], context=[{'role':'user','content':'What is on my calendar this month?'},{'role':'assistant','content':'October has five appointments. [Tools: get_upcoming]'}])

raw=json.dumps(cases,indent=2,ensure_ascii=False)+'\n'
(OUT/'corpus.json').write_text(raw)
(OUT/'corpus.sha256').write_text(hashlib.sha256(raw.encode()).hexdigest()+'\n')
print({s:sum(c['split']==s for c in cases) for s in ['dev','test','challenge']},'families',len({c['family'] for c in cases}))
