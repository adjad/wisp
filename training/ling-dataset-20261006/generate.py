#!/usr/bin/env python3
"""Deterministic, CPU-only synthetic corpus. No model or network dependencies."""
import calendar
import datetime as dt
import hashlib
import json
import random
import re
import shutil
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONTRACT = ROOT.parent / 'ling-brev-20261006'
SEED = 610120261006
VERSION = '1.1.0'
SPLITS = {'train': (1400,600), 'dev': (140,60), 'final': (210,90)}
SUMMARY_SYSTEM = '''You are Wisp. Summarize only the synthetic source results supplied in this conversation. Never invent facts, access, completeness, urgency, authorization or an action. Attribute facts to their source and speaker. Empty successful results mean no matching items in the stated coverage, not that no items exist. Denied, timeout, missing, partial and truncated results must be described honestly. Give a concise, readable overview; distinguish proposals from confirmed facts and drafts from sent messages. Do not execute or imply an outbound action.'''
# Each row is a behavioral category with separately authored split-specific surfaces.
# Surfaces belong to their split BEFORE slot expansion. Category reuse is intentional.
ROUTES = {
'agenda_week': [
 ['whats on my agnda {named}?', 'Give me the {named} agenda, including things I need to do.', 'Can I get a quick {named} sched + to-do overview?'],
 ['Help me orient to {named}: appointments and reminders together, please.'],
 ['For {named}, lay out the commitments and reminder list I have.', 'I need a sense of the agenda across my calendar and reminders for {named}.']],
'calendar_day': [
 ['calendar for {day}, pls', 'Show my appointments {day}.', 'What does {day} look like on my calendar?'],
 ['Take a look at the calendar covering {day} and give the outline.'],
 ['Could you walk through the scheduled events for {day}?', 'Pull together a calendar recap for {day}, if you can.']],
'calendar_month': [
 ['Summarize my calendar in {monthword}.', '{monthword} calendar overview?', 'What appointments do I have during {monthword}?'],
 ['Using {monthword} as the month, sketch my calendar commitments.'],
 ['I want the big picture of events scheduled in {monthword}.', 'Gather the calendar highlights that fall within {monthword}.']],
'calendar_literal_date': [
 ['Find calendar events matching {literal} on {date}.', 'Search the calendar for {literal}, date {date}.', 'Show the event named {literal} on {date}.'],
 ['Locate the calendar entry with this search text: {literal}; restrict it to {date}.'],
 ['On {date}, retrieve calendar records for the literal phrase {literal}.', 'The event I need is on {date}; look it up by {literal}.']],
'calendar_range': [
 ['Calendar overview from {start} through {end}.', 'What is on my calendar between {start} and {end}, inclusive?', 'Show my calendar {start} to {end}.'],
 ['Cover both endpoints in a calendar rundown: {start}–{end}.'],
 ['Review the scheduled calendar commitments spanning {start} through {end}.', 'Start at {start}, end at {end}, and outline all calendar events in that interval.']],
'email_overview': [
 ['Summarize unread email {named}, limit {count}, account {account}.', 'Catch me up on unread mail in {account} for {named}; show {count}.', 'Give me {count} unread email highlights {named} from my {account} account.'],
 ['Use {account} for a {named} inbox digest covering {count} unread emails.'],
 ['In account {account}, assemble an unread-mail overview for {named}, capped at {count}.', 'I want an inbox recap: {named}, unread only, {count} emails, using {account}.']],
'email_literal': [
 ['Find emails about {literal} over the last {days} {day_unit}.', 'Search my email for {literal}; last {days} {day_unit} only.', 'Read emails matching {literal} from the past {days} {day_unit}.'],
 ['Retrieve mail with the exact search text {literal}, looking back {days} {day_unit}.'],
 ['The mail lookup term is {literal}; include only the preceding {days} {day_unit}.', 'Over a {days}-day lookback, locate email records for {literal}.']],
'messages_overview': [
 ['Catch me up on messages in {group} {named}, limit {count}.', 'Summarize {count} messages with {group} {named}.', 'What happened in {group} {named}? Message overview, {count} max.'],
 ['Give a {named} conversation digest for {group} using at most {count} messages.'],
 ['From the conversation named {group}, outline {named} in up to {count} messages.', 'For {group}, gather the message highlights covering {named}; cap them at {count}.']],
'messages_search': [
 ['Search messages for {literal} on {date}, limit {count}.', 'Find {count} messages matching {literal}, dated {date}.', 'Message lookup: {literal}, {date}, {count} results.'],
 ['Retrieve message records on {date} using {literal} as the search term; maximum {count}.'],
 ['Within {date}, locate messages containing the exact phrase {literal}, up to {count}.', 'The message search should use {literal}, return {count} at most, and cover {date}.']],
'notes_records': [
 ['Find {count} notes about {literal} in {monthword}.', 'Search notes for {literal}, month {monthword}, limit {count}.', 'Show notes matching {literal} during {monthword}, {count} max.'],
 ['Look through {monthword} notes for the term {literal}; retrieve up to {count}.'],
 ['Pull note records for {literal}, restricted to {monthword} and capped at {count}.', 'Use {literal} to locate no more than {count} notes written in {monthword}.']],
'reminders_scope': [
 ['Show {scope} reminders about {literal}.', 'Find reminders matching {literal}; scope {scope}.', 'Read my {scope} reminder items for {literal}.'],
 ['Retrieve the reminders in scope {scope} using the literal search phrase {literal}.'],
 ['Within my {scope} reminders, locate entries that match {literal}.', 'I need a reminder lookup for {literal}, limited to the {scope} list.']],
'calendar_free': [
 ['Find a {minutes}-minute free slot {named} on my calendar.', 'When am I free for {minutes} minutes {named}?', 'Calendar availability {named}, need {minutes} minutes.'],
 ['Inspect calendar availability during {named} for a gap lasting {minutes} minutes.'],
 ['Identify openings in the {named} calendar long enough for {minutes} minutes.', 'A {minutes}-minute gap is what I need; check free time on the calendar for {named}.']],
'agenda_exclusion': [
 ['Agenda {named}, but skip reminders.', 'Show my {named} agenda; no reminders.', 'Give me the {named} agenda without reminders.'],
 ['Outline the agenda for {named}, excluding reminders from the sources.'],
 ['Keep reminders out when gathering my agenda covering {named}.', 'For the {named} agenda, leave the reminder source aside.']],
'multi_source': [
 ['Summarize calendar, email and messages {named}.', 'Catch me up {named} using calendar, messages, and email.', 'Give a {named} overview of my mail, calendar, and messages.'],
 ['Combine the three requested sources—messages, email, calendar—into a {named} digest.'],
 ['Prepare a {named} recap drawing from calendar plus email plus messages.', 'Review my calendar and my email alongside my messages, all covering {named}.']],
'same_email': [
 ['Only {count} this time.', 'Keep the same email filters but cap it at {count}.', 'Same mail request, {count} max now.'],
 ['Continue that email overview with its existing filters and a {count}-item ceiling.'],
 ['For that inbox digest again, retain the criteria and return at most {count}.', 'Repeat the mail summary we were discussing, lowering the result limit to {count}.']],
'switch_email_calendar': [
 ['Actually, calendar instead.', 'No, show my calendar.', 'Switch that to a calendar overview.'],
 ['Change the source to calendar and give its general overview.'],
 ['I meant my schedule; summarize the calendar source instead.', 'Replace the email request with a calendar digest.']],
'switch_calendar_messages': [
 ['Actually, messages with {group}.', 'Make that a message overview in {group}.', 'No, catch me up on {group} messages.'],
 ['Use messages as the corrected source, specifically the conversation {group}.'],
 ['Replace the calendar lookup with highlights from the {group} message thread.', 'What I intended was a conversation recap for {group}; switch to messages.']],
'inline_draft': [
 ['Draft an email saying {literal}.', 'Write a message here that says {literal}.', 'Help me word a calendar invitation about {literal}.'],
 ['Compose wording I could paste into an email: {literal}.'],
 ['Suggest an unsent message draft with this point: {literal}.', 'Put together text for a proposed invitation about {literal}, here in chat.']],
'effect': [
 ['Send an email to {person} saying {literal}.', 'Create a calendar event called {literal} on {date}.', 'Delete the reminder named {literal}.'],
 ['Save a new note containing {literal}.'],
 ['Post {literal} to the {group} message conversation.', 'Move the calendar event {literal} to {date}.']],
'prohibition': [
 ['Do not read my email.', 'Leave my messages alone.', 'No calendar access, please.'],
 ['Please refrain from looking through reminders.'],
 ['Keep all my notes unopened.', 'I am prohibiting email and message reads.']],
'web_unsupported': [
 ['Search the web for {literal}.', 'Look up {literal} online.', 'Give a web news digest about {literal}.'],
 ['Use an internet search to find information on {literal}.'],
 ['Browse websites for a current overview of {literal}.', 'Retrieve results from the public web matching {literal}.']],
'calendar_location': [
 ['Calendar {named}, only {constraint}.', 'Show appointments {named} with the filter {constraint}.', 'Summarize my calendar {named}; require {constraint}.'],
 ['Apply the constraint {constraint} to the calendar overview for {named}.'],
 ['The calendar digest for {named} should satisfy {constraint}.', 'For scheduled events covering {named}, include this restriction: {constraint}.']],
'email_constraint': [
 ['Email highlights {named}, {constraint}.', 'Summarize mail {named}; {constraint}.', 'Inbox overview for {named} with {constraint}.'],
 ['For the {named} email digest, impose {constraint}.'],
 ['Gather a mail overview covering {named}, subject to {constraint}.', 'I need an email recap during {named} that applies {constraint}.']],
'reminder_time': [
 ['Summarize reminders {named}.', 'What reminders do I have {named}?', 'Reminder overview for {named}, please.'],
 ['Gather the reminder list covering {named}.'],
 ['Tell me about reminder commitments falling in {named}.', 'Give a digest of my reminders within {named}.']],
'messages_unread': [
 ['Summarize unread messages with {group}.', 'Catch me up on {group}, unread messages only.', 'Show message highlights in {group}; unread only.'],
 ['Limit the conversation overview for {group} to unread messages.'],
 ['In {group}, prepare a message recap with an unread-only filter.', 'Retrieve the unread portion of messages with {group} as an overview.']],
'mixed_effect': [
 ['Read my email and send {person} a recap.', 'Check my calendar and create an event for {literal}.', 'Look at messages then post {literal} to {group}.'],
 ['Summarize reminders, then delete the ones titled {literal}.'],
 ['Read notes matching {literal} and save an edited copy.', 'Review the inbox and forward the digest to {person}.']],
'pure_exclusions': [
 ['No email or messages.', 'Exclude calendar and reminders.', 'Skip notes and web.'],
 ['Please omit the email and calendar sources entirely.'],
 ['Leave messages and reminders out of any access.', 'Keep web and notes excluded.']],
'same_messages': [
 ['Make that {count} messages.', 'Same conversation, limit {count}.', 'Continue with the same message filters, {count} max.'],
 ['For the conversation just requested, retain its scope and cap the digest at {count}.'],
 ['Repeat those message highlights using the existing conversation and time, limited to {count}.', 'Stay with that thread and period; return no more than {count} messages this time.']],
'same_calendar': [
 ['Use {start} through {end} instead.', 'Same calendar search, dates {start} to {end}.', 'Change just the calendar dates to {start}–{end}.'],
 ['Retain that calendar search text and replace the time window with {start} through {end}.'],
 ['For the existing calendar lookup, update its inclusive interval to {start}–{end}.', 'Keep the event search term while shifting the calendar range to {start} through {end}.']],
'agenda_typos': [
 ['whats my agnda tmrw?', 'anythng on my agenda today?', 'agenda for tmr pls'],
 ['pls lay out tomorrows agenda, calandar n reminders'],
 ['could u give the agenda for tomorrow, appointments plus to dos', 'id like todays agnda with calendar and reminder stuff']]
}
# Additional natural agenda surfaces were authored before slot expansion for D3.
ROUTES['agenda_week'][0] += [
 'can u run thru my agenda {named}?', 'need the {named} plan, calendar + reminders',
 'How is my agenda looking for {named}?', 'show me whats lined up {named}: events and reminders',
 'gimme my agenda {named}', 'quick peek at my agenda for {named}',
 'any appointments or reminder tasks {named}?', 'Help me plan {named} using my calendar and reminders.',
 '{named} to-dos and events?', 'im figuring out {named}; whats on the agenda',
 'pull up the calandar and reminder rundown {named}', 'whats my schedule n reminder tasks for {named}',
 'lets see the agenda {named}']
ROUTES['calendar_day'][0] += [
 'what did i put on the cal for {day}?', 'any calendar stuff {day}?', 'pull up the {day} calandar',
 'can u check what i have booked {day}?', 'run thru my {day} calendar pls',
 '{day} appointments, quick look?', 'whats scheduled {day} on the cal?',
 'need a rundown of {day} calendar events', 'peek at my schedule {day}']
ROUTES['calendar_month'][0] += [
 'how busy is my calendar in {monthword}?', 'calendar stuff for {monthword}, quick rundown pls',
 'can u list the appointments across {monthword}?']
ROUTES['agenda_typos'][0] += [
 'today agenda pls, cal and reminders', 'any plans n reminder tasks today?',
 'whats on todays agenda again?', 'show me tomorrow agenda cal n to dos',
 'can u check tomorrow agenda for me', 'todays appts and reminders quick overview',
 'need my tomorrow rundown with calandar and reminders', 'wats my agenda today?',
 'tomorrow plans plus reminder tasks?']
ROUTES['agenda_day']=[
 ['can i see my {agenda_day} agenda?', 'whats my {agenda_day} plan, calendar n reminders?',
  'anything on the agenda {agenda_day}?', 'gimme the {agenda_day} agenda real quick',
  'need {agenda_day} appointments and reminder tasks', 'how is {agenda_day} looking on my agenda?',
  'run thru calendar + reminders for {agenda_day}', 'show the agenda for {agenda_day}, pls'],
 ['Lay out the agenda I have for {agenda_day}: events together with reminders.',
  'I am planning {agenda_day}; gather my calendar and reminder commitments.'],
 ['For {agenda_day}, give an outline of the personal agenda from calendar and reminders.',
  'Walk me through the agenda covering {agenda_day}, with its reminder list.',
  'I would like an integrated {agenda_day} agenda using appointments and reminders.']]
ROUTES['agenda_informal']=[
 ['whats my agnda for {named}?', 'can u do the agenda for {named} pls',
  'how my calndr n reminders look {named}?', 'agenda {named}, quick look pls',
  'wats coming up on my agenda {named}', 'i need the agenda for {named} rn',
  'run thru {named} agenda w calendar + reminders', 'got stuff on my agnda {named}?',
  'hey check my agenda {named}', 'whats lined up for {named} on the agenda'],
 ['Could u pull together my {named} agenda incl calendar and reminders?',
  'For {named}, pls tell me what the personal agenda contains.'],
 ['Looking for an agenda recap covering {named}; calendar n reminders please.',
  'I wanna understand the {named} agenda using scheduled stuff and reminder tasks.',
  'Would u outline my commitments from the agenda during {named}?']]

ROUTES.update({
 'unfiltered_read':[
  ['Summarize my {read_domain}.','Quick {read_domain} overview pls.','Catch me up using {read_domain}.','Show my {read_domain} highlights.','Read my {read_domain} and give an overview.'],
  ['Use the {read_domain} source for a general digest.','Tell me the overall picture in {read_domain}.'],
  ['Gather a concise rundown from my {read_domain}.','I want the general overview available from {read_domain}.','Could you outline the contents of my {read_domain}?']],
 'daily_read':[
  ['Summarize my {read_domain} {agenda_day}.','Quick {agenda_day} {read_domain} recap.','Read {read_domain} for {agenda_day}, pls.','Whats in my {read_domain} {agenda_day}?'],
  ['Give an outline from {read_domain} covering {agenda_day}.'],
  ['For {agenda_day}, review the {read_domain} source and return its digest.']],
 'email_read_false':[
  ['Summarize already-read email {named}.','Email digest {named}, marked read only.','Show emails I have already read {named} as an overview.'],
  ['Limit the {named} inbox overview to emails marked read.'],
  ['Give a {named} mail recap with unread set to false.','Only already-read emails belong in my {named} overview.']],
 'calendar_rolling':[
  ['Calendar overview for the next {rolling_days} days.','Show appointments over the coming {rolling_days} days.','What is on my calendar in the next {rolling_days} days?'],
  ['Review the calendar using a rolling window of {rolling_days} days starting now.'],
  ['Look ahead {rolling_days} days for a calendar recap.','Outline events across the forthcoming {rolling_days} calendar days.']],
 'email_person':[
  ['Find emails from {person}.','Search email for {person}.','Show mail sent by {person}.'],
  ['Retrieve email records using the sender name {person}.'],
  ['Locate mail from the person named {person}.','Use {person} as the literal email lookup term.']],
 'messages_person':[
  ['Summarize messages with {person}.','Catch me up on my conversation with {person}.','What happened in my messages with {person}?'],
  ['Give a message overview for the conversation with {person}.'],
  ['Outline the message highlights exchanged with {person}.','I want a conversation digest from my messages with {person}.']],
 'calendar_account':[
  ['Calendar overview {named} from my {account} account.','Show {named} appointments in the {account} account.','Use my {account} calendar account for a {named} overview.'],
  ['Restrict the {named} calendar digest to the account named {account}.'],
  ['In calendar account {account}, outline the events covering {named}.','My {account} account is the one to use for the {named} calendar rundown.']],
 'ordinary_none':[
  ['Thanks!','Hi Wisp.','That makes sense.','How are you?','Explain what a reminder is.','What does the word agenda mean?','I will think about it.'],
  ['Understood, I appreciate the explanation.','Define calendar without opening mine.'],
  ['Good morning, Wisp.','That answered my question.','Tell me what an inbox means.']]
})

NAMES = {'train':['Avery','Mina','Jonah','Samir','Lena','Theo','Nora','Isha'], 'dev':['Pavel','Keiko','Omar','Ruth','Nico','Esme'], 'final':['Daria','Malik','Yuna','Hugo','Cleo','Tariq']}
LITERALS = {'train':['Orchid launch','budget sketch','Atlas handoff','garden rota','Q3 planning','café review','travel receipts','design sync','+avery@sample.invalid','API v2 + review'], 'dev':['Copper delivery','supply ledger','Maple workshop','museum tickets','+pavel@demo.invalid','draft: final?'], 'final':['Harbor rehearsal','lab inventory','Juniper migration','volunteer roster','+daria@test.invalid','Proposal + 1:1']}
NAMED = ['this week','next week','last week','this weekend','next weekend','this month','next month','last month']
DAYS = ['today','tomorrow','yesterday','tonight']
DOMAINS = ['calendar','reminders','email','messages','notes']

def dumps(x): return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def message(role,content,final=False):
    v={'role':role,'content':content}
    if role=='assistant': v['training']=final
    return v

def route_slots(split,i):
    rng=random.Random(SEED+{'train':0,'dev':100000,'final':200000}[split]+i*7919)
    person=rng.choice(NAMES[split])
    year=2027+{'train':0,'dev':1,'final':2}[split]
    month=rng.randint(1,12); day=rng.randint(1,24)
    date=f'{year}-{month:02d}-{day:02d}'
    start=f'{year}-{month:02d}-{rng.randint(1,10):02d}'; end=f'{year}-{month:02d}-{rng.randint(12,28):02d}'
    return dict(person=person,literal=rng.choice(LITERALS[split]),group=f'{person} project circle',
        read_domain=rng.choice(DOMAINS),rolling_days=rng.choice([1,2,3,5,7,10,14,21,30,45,60]),account=rng.choice(['work','personal','research']),date=date,start=start,end=end,month=date[:7],
        monthword=f'{calendar.month_name[month]} {year}',named=rng.choice(NAMED),day=rng.choice(DAYS),
        count=rng.choice([1,3,5,8,12,20,35,50,100]),days=rng.choice([1,2,7,14,30,90,180,366]),
        minutes=rng.choice([15,25,30,45,60,90,120,1440]),agenda_day=rng.choice(['today','tomorrow']),scope=rng.choice(['all','today','tomorrow','overdue','upcoming']),
        constraint=rng.choice(['in the library','outside the office','marked completed',f'excluding {person}']))

def request(domain,operation='overview',**filters): return {'domain':domain,'operation':operation,'filters':filters}

def route_case(category,template_index,s,split):
    t=ROUTES[category][{'train':0,'dev':1,'final':2}[split]][template_index]
    if category in {'email_overview','same_email','messages_overview','same_messages','multi_source','email_constraint','email_read_false'}:
        s['named']=['today','yesterday','this week','last week','this month','last month'][int(s['date'][-2:])%6]
    elif category=='calendar_free':
        s['named']=['today','tomorrow','this week','next week','this weekend','next weekend'][int(s['date'][-2:])%6]
    elif category=='agenda_informal':
        s['named']=['this week','next week','this month','next month'][int(s['date'][-2:])%4]
    if category=='daily_read' and s['read_domain'] in {'email','messages','notes'}:s['agenda_day']='today'
    s['day_unit']='day' if s['days']==1 else 'days'
    user=t.format(**s); history=[]; req=[]; exclusions=[]; unsupported=[]; action='read'
    named={'named':s['named']}; day={'named':s['day']}; date={'date':s['date']}; interval={'start':s['start'],'end':s['end']}
    if category=='unfiltered_read': req=[request(s['read_domain'])]
    elif category=='daily_read': req=[request(s['read_domain'],**({'scope':s['agenda_day']} if s['read_domain']=='reminders' else {'time':{'named':s['agenda_day']}}))]
    elif category=='email_read_false': req=[request('email',time=named,unread=False)]
    elif category=='calendar_rolling': req=[request('calendar',time={'rolling_days':s['rolling_days']})]
    elif category=='email_person': req=[request('email','records',query=s['person'])]
    elif category=='messages_person': req=[request('messages',conversation=s['person'])]
    elif category=='calendar_account': req=[request('calendar',time=named,account=s['account'])]
    elif category=='ordinary_none':
        action='none'
        if 'Define calendar without opening mine.'==user:exclusions=['calendar']
    elif category in {'agenda_week','agenda_informal'}: req=[request('calendar',time=named),request('reminders')]; unsupported=[s['named']]
    elif category=='agenda_day': req=[request('calendar',time={'named':s['agenda_day']}),request('reminders',scope=s['agenda_day'])]
    elif category=='calendar_day': req=[request('calendar',time=day)]
    elif category=='calendar_month': req=[request('calendar',time={'month':s['month']})]
    elif category=='calendar_literal_date': req=[request('calendar','records',query=s['literal'],time=date)]
    elif category=='calendar_range': req=[request('calendar',time=interval)]
    elif category=='email_overview': req=[request('email',time=named,unread=True,count=s['count'],account=s['account'])]
    elif category=='email_literal': req=[request('email','records',query=s['literal'],time={'last_n_days':s['days']})]
    elif category=='messages_overview': req=[request('messages',time=named,conversation=s['group'],count=s['count'])]
    elif category=='messages_search': req=[request('messages','records',time=date,query=s['literal'],count=s['count'])]
    elif category=='notes_records': req=[request('notes','records',query=s['literal'],time={'month':s['month']},count=s['count'])]
    elif category=='reminders_scope': req=[request('reminders','records',scope=s['scope'],query=s['literal'])]
    elif category=='calendar_free': req=[request('calendar','free_time',time=named,minutes=s['minutes'])]
    elif category=='agenda_exclusion': req=[request('calendar',time=named)]; exclusions=['reminders']
    elif category=='multi_source': req=[request(d,time=named) for d in ['calendar','email','messages']]
    elif category in ('same_email','switch_email_calendar'):
        prior=request('email',time=named,unread=True,account=s['account'],count=8)
        history=[message('user',f'Summarize unread email {s["named"]} in account {s["account"]}, limit 8.'),message('assistant',dumps({'version':1,'kind':'read','sources':[dict(domain=prior['domain'],operation=prior['operation'],**prior['filters'])],'excluded_sources':[],'unsupported_constraints':[]}))]
        req=[request('email',time=named,unread=True,account=s['account'],count=s['count'])] if category=='same_email' else [request('calendar')]
    elif category=='switch_calendar_messages':
        history=[message('user',f'Find calendar entries for {s["literal"]} on {s["date"]} in account {s["account"]}.'),message('assistant','The previous source was calendar.',False)]
        req=[request('messages',conversation=s['group'])]
    elif category=='inline_draft': action='inline'
    elif category in ('effect','mixed_effect','web_unsupported'): action='unsupported'
    elif category in ('prohibition','pure_exclusions'):
        action='none'
        patterns={'email':'email','messages':'message','calendar':'calendar','reminders':'reminder','notes':'note','web':'web'}
        exclusions=[d for d,p in patterns.items() if p in user.lower()]
    elif category=='calendar_location': req=[request('calendar',time=named)]; unsupported=[s['constraint']]
    elif category=='email_constraint': req=[request('email',time=named)]; unsupported=[s['constraint']]
    elif category=='reminder_time': req=[request('reminders')]; unsupported=[s['named']]
    elif category=='messages_unread': req=[request('messages',conversation=s['group'])]; unsupported=[next(x for x in ['unread messages only','unread messages','unread only','unread-only filter','unread portion'] if x in user)]
    elif category=='same_messages':
        history=[message('user',f'Summarize messages with {s["group"]} {s["named"]}, limit 8.'),message('assistant','Messages was the previous source.',False)]
        req=[request('messages',conversation=s['group'],time=named,count=s['count'])]
    elif category=='same_calendar':
        history=[message('user',f'Find calendar events matching {s["literal"]} on {s["date"]}.'),message('assistant','Calendar was the previous source.',False)]
        req=[request('calendar','records',query=s['literal'],time=interval)]
    elif category=='agenda_typos':
        is_today='today' in user or 'todays' in user
        req=[request('calendar',time={'named':'today' if is_today else 'tomorrow'}),request('reminders',scope='today' if is_today else 'tomorrow')]
    else: raise ValueError(category)
    # Unsupported reminder periods remain explicit; day agendas use supported reminder scope.
    facts={'action':action,'requests':req,'excluded':exclusions,'unsupported':unsupported,'context_relation': 'same_source' if category.startswith('same_') else 'source_switch' if category.startswith('switch_') else 'new_request'}
    return user,history,facts,t

def target_from_facts(f):
    return {'version':1,'kind':f['action'],'sources':[dict(domain=r['domain'],operation=r['operation'],**r['filters']) for r in f['requests']], 'excluded_sources':f['excluded'],'unsupported_constraints':f['unsupported']}

SUMMARY_CATEGORIES = ['calendar','agenda','email','messages','multi','reminders','notes','empty','denied','timeout','partial','truncated','missing','proposal','overlap','zones','mixed_status','no_deadline','weekly_agenda','draft_sent']
# Compositional profiles are split-owned as well as the linguistic surfaces.
SUMMARY_FORMS = {
 'train':['Give me a quick {topic} overview from these results.','Catch me up on {topic}; keep it brief and grounded in the supplied results.'],
 'dev':['Help me understand the {topic} picture represented by the source returns below.'],
 'final':['Using the returned source data, outline what I should know about {topic}.']}
TOPICS={'calendar':'calendar','agenda':'agenda','email':'email','messages':'messages','multi':'calendar, email and messages','reminders':'reminders','notes':'notes','empty':'calendar','denied':'email','timeout':'messages','partial':'email','truncated':'messages','missing':'calendar and email','proposal':'messages','overlap':'calendar','zones':'calendar','mixed_status':'calendar, email and messages','no_deadline':'email','weekly_agenda':'weekly agenda','draft_sent':'email draft and sent records'}
SUMMARY_ITEMS={'train':2,'dev':3,'final':4}

# Topic values and verbs produce useful varied fixture content; all are synthetic.
ACTIVITIES = {
 'train':['prototype review','room booking','orchid order','equipment check','accessibility walkthrough','budget review','garden roster','poster proof','volunteer orientation','receipt review','release rehearsal','library tour'],
 'dev':['storage survey','specimen transfer','print inspection','archive intake','lab cleanup','route planning','venue walkthrough','grant outline'],
 'final':['harbor survey','donation sorting','instrument calibration','catalogue audit','trail inspection','workshop setup','translation review','supply pickup']}


def summary_case(category,form_i,s,split,i):
    name=s['person']; subject=ACTIVITIES[split][i%len(ACTIVITIES[split])]
    date=s['date']; count=SUMMARY_ITEMS[split]
    sources=[]; clauses=[]; facts=[]; caveats=[]
    def add(domain,status,items,coverage=None,**extra):
        src={'source':domain,'status':status,'coverage':coverage or {'date':date,'timezone':'America/Los_Angeles'},'items':items,**extra}
        sources.append(src); return src
    def fact(source,field,value,item_id): facts.append({'source':source,'field':field,'value':value,'item_id':item_id})
    def calendar_rows(n=count,overlap=False,zones=False):
        rows=[]
        for j in range(n):
            hour=9+j*2 if not overlap else 9
            title=f'{subject} { ["check-in","briefing","review","handoff"][j%4] }'
            zone=['America/Los_Angeles','America/New_York'][j%2] if zones else 'America/Los_Angeles'
            row={'id':f'cal-{j}','title':title,'date':date,'start':f'{hour:02d}:00','end':f'{hour:02d}:45','timezone':zone,'location':['Studio 4','Lab B','Room 12','Annex'][j%4]}
            rows.append(row); fact('calendar','title',title,row['id']); fact('calendar','start',row['start'],row['id'])
        return rows
    def calendar_text(rows):
        return 'Calendar: '+ '; '.join(f'{r["title"]} at {r["start"]} ({r["timezone"]})' for r in rows)+f' on {date}.'
    def mail_rows(n=count,deadline=True):
        rows=[]
        for j in range(n):
            sender=NAMES[split][(i+j)%len(NAMES[split])]
            item={'id':f'email-{j}','sender':sender,'subject':f'{subject}: { ["materials","arrival","cost","review"][j%4] }','received_date':date,'body':f'{sender} reports that { ["the materials are ready","arrival is set for 14:00","the estimate is 240 credits","review is requested"][j%4] }.','sent':False}
            if deadline: item['requested_by']=f'{s["month"]}-{min(int(date[-2:])+2,28):02d}'
            rows.append(item); fact('email','sender',sender,item['id']); fact('email','subject',item['subject'],item['id']); fact('email','body',item['body'],item['id'])
        return rows
    def mail_text(rows):
        return f'Email (work account, unread, {date}; {len(rows)} returned):\n'+ '\n'.join('- '+r['subject']+': '+r['body']+(f' Requested by {r["requested_by"]}.' if 'requested_by' in r else '') for r in rows)
    def message_rows(n=count,proposal=False):
        rows=[]
        for j in range(n):
            speaker=NAMES[split][(i+j)%len(NAMES[split])]
            if proposal and j==0: body=f'Could we move the {subject} to 15:00? This is only a proposal.'; state='proposal'
            elif proposal and j==1: body='I have not agreed to a new time.'; state='unconfirmed'
            else: body=f'{ ["I can bring the materials","I will check the room","I need the draft outline","I can review the checklist"][j%4] } for {subject}.'; state='statement'
            row={'id':f'msg-{j}','speaker':speaker,'conversation':s['group'],'date':date,'body':body,'state':state}
            rows.append(row); fact('messages','speaker',speaker,row['id']); fact('messages','body',body,row['id'])
        return rows
    def message_text(rows):
        lines=[]
        for r in rows:
            body=r['body'];speaker=r['speaker']
            if r['state']=='proposal':text=f'{speaker} proposed moving the {subject} to 15:00.'
            elif r['state']=='unconfirmed':text=f'{speaker} has not agreed to a new time.'
            elif body.startswith('I can bring'):text=f'{speaker} offered to bring the materials for {subject}.'
            elif body.startswith('I will check'):text=f'{speaker} will check the room for {subject}.'
            elif body.startswith('I need'):text=f'{speaker} needs the draft outline for {subject}.'
            else:text=f'{speaker} offered to review the checklist for {subject}.'
            lines.append('- '+text)
        return f'Messages ({s["group"]}, {date}; {len(rows)} returned):\n'+'\n'.join(lines)
    def reminder_rows(n=count):
        rows=[]
        for j in range(n):
            r={'id':f'rem-{j}','title':f'{["Prepare","Check","Return","Confirm"][j%4]} {subject}','due_date':date if j%2==0 else None,'completed':False}
            rows.append(r); fact('reminders','title',r['title'],r['id'])
        return rows
    def reminder_text(rows): return 'Reminders: '+ '; '.join(f'{r["title"]}'+(f' due {r["due_date"]}' if r['due_date'] else ' (no due date supplied)') for r in rows)+'.'
    if category=='weekly_agenda':
        base=dt.date.fromisoformat(date);week_end=(base+dt.timedelta(days=6)).isoformat()
        rows=calendar_rows();rem=reminder_rows(count-1)
        for j,row in enumerate(rows):row['date']=(base+dt.timedelta(days=j*2)).isoformat()
        for j,row in enumerate(rem):row['due_date']=(base+dt.timedelta(days=j*2+1)).isoformat()
        add('calendar','ok',rows,{'start':date,'end':week_end,'timezone':'America/Los_Angeles'})
        add('reminders','ok',rem,{'scope':'all','returned_due_dates':'explicit dates in items; not an arbitrary-time source filter'})
        lines=[f'Weekly agenda ({date}–{week_end}; calendar times in America/Los_Angeles):']
        for day_value in sorted({r['date'] for r in rows}|{r['due_date'] for r in rem}):
            events=[f'{r["title"]} at {r["start"]} ({r["timezone"]})' for r in rows if r['date']==day_value]
            tasks=[r['title'] for r in rem if r['due_date']==day_value]
            parts=[]
            if events:parts.append('Calendar: '+'; '.join(events))
            if tasks:parts.append('Reminders due: '+'; '.join(tasks))
            lines.append(day_value+' — '+'; '.join(parts)+'.')
        clauses=['\n'.join(lines)]
    elif category=='draft_sent':
        rows=mail_rows(2,deadline=False)
        for j,row in enumerate(rows):row['delivery_state']='draft' if j==0 else 'sent';row['sent']=bool(j)
        add('email','ok',rows,{'date':date,'account':'work','kind':'draft and sent records'})
        clauses=[f'Email records (work account, {date}):\n'+'\n'.join(f'- {r["delivery_state"].capitalize()}: {r["subject"]}. {r["body"]}' for r in rows)]
        caveats=['draft_sent_distinction']
    elif category in ['calendar','overlap','zones']:
        rows=calendar_rows(overlap=category=='overlap',zones=category=='zones'); add('calendar','ok',rows); clauses=[calendar_text(rows)]
        if category=='overlap': clauses.append('These calendar events overlap: they all run 09:00–09:45.'); caveats=['overlap']
        if category=='zones': clauses.append('The listed times use different time zones; they are not all local Los Angeles times.'); caveats=['mixed_timezones']
    elif category=='agenda':
        rows=calendar_rows(); add('calendar','ok',rows); rem=reminder_rows(count-1); add('reminders','ok',rem,{'scope':'all'}); clauses=[calendar_text(rows),reminder_text(rem)]
    elif category in ['email','no_deadline']:
        rows=mail_rows(deadline=category!='no_deadline'); add('email','ok',rows,{'date':date,'unread':True,'account':'work'}); clauses=[mail_text(rows)]
        if category=='no_deadline': clauses.append('No deadline is stated in these emails.'); caveats=['no_deadline']
    elif category in ['messages','proposal']:
        rows=message_rows(proposal=category=='proposal'); add('messages','ok',rows,{'date':date,'conversation':s['group']}); clauses=[message_text(rows)]
        if category=='proposal': clauses.append('The proposed move is unconfirmed; no schedule change is established here.'); caveats=['proposal_unconfirmed']
    elif category=='multi':
        cr=calendar_rows(1 if split=='train' else 2); er=mail_rows(1 if split!='final' else 2); mr=message_rows(1 if split=='dev' else 2)
        add('calendar','ok',cr);add('email','ok',er,{'date':date,'account':'work','unread':True});add('messages','ok',mr,{'date':date,'conversation':s['group']});clauses=[calendar_text(cr),mail_text(er),message_text(mr)]
    elif category=='reminders':
        rows=reminder_rows();add('reminders','ok',rows,{'scope':'all'});clauses=[reminder_text(rows)]
    elif category=='notes':
        rows=[]
        for j in range(count):
            row={'id':f'note-{j}','title':f'{subject} { ["outline","checklist","draft","reference"][j%4]}','updated_date':date,'text':f'{["Materials: 6 folders","Room: Annex","Budget estimate: 240 credits","Owner: "+name][j%4]}. Status: draft.'}
            rows.append(row);fact('notes','title',row['title'],row['id']);fact('notes','text',row['text'],row['id'])
        add('notes','ok',rows);clauses=['Notes: '+' '.join(f'{r["title"]}: {r["text"]}' for r in rows)];caveats=['draft_notes']
    elif category=='empty':
        add('calendar','ok',[],{'date':date,'query':subject,'account':'work'});clauses=[f'Calendar returned no matching events for {subject} on {date} in the work account.'];caveats=['empty_scoped']
    elif category=='denied':
        add('email','denied',[],{'date':date,'account':'work'},reason='permission not granted');clauses=['Email access was denied, so I cannot summarize the requested inbox results.'];caveats=['denied']
    elif category=='timeout':
        add('messages','timeout',[],{'date':date,'conversation':s['group']},reason='source request timed out');clauses=['The messages request timed out; I do not have a conversation overview from it.'];caveats=['timeout']
    elif category=='partial':
        rows=mail_rows();add('email','partial',rows,{'date':date,'account':'work','unread':True},missing_part='archived folder',complete=False);clauses=[mail_text(rows),'This email overview is partial: the archived folder was not returned.'];caveats=['partial']
    elif category=='truncated':
        rows=message_rows();add('messages','truncated',rows,{'date':date,'conversation':s['group']},returned_count=len(rows),total_matching=len(rows)+7,complete=False);clauses=[message_text(rows),f'The message results are truncated: {len(rows)} of {len(rows)+7} matching messages were returned.'];caveats=['truncated']
    elif category=='missing':
        rows=calendar_rows();add('calendar','ok',rows);add('email','missing',[],{'date':date},reason='no source result supplied');clauses=[calendar_text(rows),'No email result was supplied, so the email portion is unavailable.'];caveats=['missing']
    elif category=='mixed_status':
        rows=calendar_rows();add('calendar','ok',rows);add('email','denied',[],reason='permission not granted');add('messages','timeout',[],reason='source request timed out');clauses=[calendar_text(rows),'Email access was denied, and messages timed out; those portions are unavailable.'];caveats=['denied','timeout']
    else: raise ValueError(category)
    form=SUMMARY_FORMS[split][form_i]
    user=form.format(topic=TOPICS[category])+'\n\nSynthetic source results:\n'+dumps({'synthetic':True,'results':sources})
    answer='\n'.join(clauses)
    rubric={'required_facts':facts,'required_caveats':caveats,'source_statuses':{r['source']:r['status'] for r in sources},'required_coverage':{r['source']:r['coverage'] for r in sources},'forbidden_claims':['action_executed','unsupported_urgency','unseen_fact','complete_when_incomplete','denied_as_empty','proposal_as_confirmed'],'evaluation':'Score factual entailment, source/speaker attribution, status/coverage honesty and concise readability; equivalent wording is allowed. No exact-text-only score.'}
    return user,answer,sources,rubric,form

def allocate(n,families):
    q,r=divmod(n,len(families));return [(fam,q+(i<r)) for i,fam in enumerate(families)]

def write_jsonl(path,rows): path.write_text(''.join(dumps(r)+'\n' for r in rows),encoding='utf-8')

def generate():
    ROOT.mkdir(exist_ok=True)
    # Copy the exact frozen contract; it is never modified in its original namespace.
    for fn in ['intent.schema.v1.json','router-system.txt']: shutil.copyfile(CONTRACT/fn,ROOT/fn)
    system=(ROOT/'router-system.txt').read_text()
    family_manifest=[]; all_meta={}
    for split,(routing_n,summary_n) in SPLITS.items():
        examples=[];metadata=[];idx=0
        fams=[(c,t) for c,forms in ROUTES.items() for t in range(len(forms[{'train':0,'dev':1,'final':2}[split]]))]
        caps={'email_read_false':6,'unfiltered_read':5,'daily_read':7,'ordinary_none':1,'agenda_week':8,'agenda_informal':4,'agenda_day':2,'calendar_day':4,'calendar_month':12,'agenda_exclusion':8,'multi_source':6,'reminder_time':8,'inline_draft':len(LITERALS[split]),'web_unsupported':len(LITERALS[split]),'prohibition':1,'pure_exclusions':1,'agenda_typos':1}
        cardinalities={'read_domain':5,'rolling_days':11,'named':8,'day':4,'agenda_day':2,'day_unit':1,'literal':len(LITERALS[split]),'person':len(NAMES[split]),'group':len(NAMES[split]),'monthword':12,'date':288,'start':120,'end':204,'account':3,'count':9,'days':8,'minutes':8,'scope':5,'constraint':3+len(NAMES[split])}
        capacities=[]
        for cat,ti in fams:
            template=ROUTES[cat][{'train':0,'dev':1,'final':2}[split]][ti]
            capacity=1
            for slot in set(re.findall(r'\{(\w+)\}',template)):capacity*=cardinalities[slot]
            if cat.startswith(('same_','switch_')):capacity=100
            capacities.append(min(100,caps.get(cat,capacity)))
        require_capacity=sum(capacities)
        if require_capacity<routing_n:raise ValueError('insufficient independently varied surface capacity')
        allocation=[0]*len(fams)
        while sum(allocation)<routing_n:
            for k,(cat,_) in enumerate(fams):
                if sum(allocation)==routing_n:break
                if allocation[k]<capacities[k]:allocation[k]+=1
        seen=set();attempt=0
        for (cat,ti),n in zip(fams,allocation):
            family=f'routing/{split}/{cat}/surface-{ti}'
            template=ROUTES[cat][{'train':0,'dev':1,'final':2}[split]][ti]
            family_manifest.append({'split':split,'task':'routing','category':cat,'family':family,'template':template,'examples':n})
            for j in range(n):
                for retry in range(10000):
                    attempt+=1
                    slots=route_slots(split,attempt);user,history,facts,_=route_case(cat,ti,slots,split)
                    key=dumps(history)+user
                    if key not in seen:seen.add(key);break
                else:raise ValueError('surface capacity exhausted: '+family)
                exid=f'{split}-r-{idx:04d}';idx+=1
                examples.append({'id':exid,'messages':[message('system',system),*history,message('user',user),message('assistant',dumps(target_from_facts(facts)),True)]})
                metadata.append({'id':exid,'split':split,'task':'routing','category':cat,'family':family,'semantic_spec':facts,'synthetic_slots':slots,'provenance':'fresh specification plus deterministic split-owned surface expansion','template':template})
        fams=[(c,t) for c in SUMMARY_CATEGORIES for t in range(len(SUMMARY_FORMS[split]))]
        idx=0; summary_seen=set(); summary_attempt=50000
        for (cat,ti),n in allocate(summary_n,fams):
            family=f'overview/{split}/{cat}/composition-{SUMMARY_ITEMS[split]}/surface-{ti}'
            family_manifest.append({'split':split,'task':'overview','category':cat,'family':family,'template':SUMMARY_FORMS[split][ti],'composition_profile':f'{split}:primary_items={SUMMARY_ITEMS[split]}','examples':n})
            for j in range(n):
                for retry in range(10000):
                    summary_attempt+=1
                    slots=route_slots(split,summary_attempt);user,answer,fixture,rubric,_=summary_case(cat,ti,slots,split,summary_attempt)
                    if user not in summary_seen:summary_seen.add(user);break
                else:raise ValueError('overview fixture capacity exhausted: '+family)
                exid=f'{split}-s-{idx:04d}';idx+=1
                examples.append({'id':exid,'messages':[message('system',SUMMARY_SYSTEM),message('user',user),message('assistant',answer,True)]})
                metadata.append({'id':exid,'split':split,'task':'overview','category':cat,'family':family,'fixture':fixture,'rubric':rubric,'synthetic_slots':slots,'template':SUMMARY_FORMS[split][ti],'provenance':'synthetic structured facts plus deterministic split-owned composition and prose'})
        # Stable mixed order. No model inference, model imports, or training occurs here.
        rng=random.Random(SEED+list(SPLITS).index(split)); order=list(range(len(examples)));rng.shuffle(order)
        examples=[examples[k] for k in order];metadata=[metadata[k] for k in order]
        write_jsonl(ROOT/f'{split}.jsonl',examples);write_jsonl(ROOT/f'{split}.provenance.jsonl',metadata)
        all_meta[split]={'total':len(examples),'routing':routing_n,'grounded_wisp_overviews':summary_n,'unrelated_general_assistant':0,'families':len(set(m['family'] for m in metadata)),'categories':dict(Counter(m['category'] for m in metadata))}
    (ROOT/'families.json').write_text(json.dumps({'partition_policy':'Split-owned linguistic surfaces and composition profiles assigned before deterministic slot expansion; broad behavioral categories deliberately reused. No human authoring or measured performance claim.','families':family_manifest},ensure_ascii=False,indent=2)+'\n')
    tracked=['train.jsonl','dev.jsonl','final.jsonl','train.provenance.jsonl','dev.provenance.jsonl','final.provenance.jsonl','intent.schema.v1.json','router-system.txt','families.json','generate.py','validate.py','DATA_CARD.md']
    manifest={'generator_version':VERSION,'seed':SEED,'base_sha':'c1ef1be4c95f4806688484f8785e36c4b06138c0','synthetic':True,'source_contract_sha256':{fn:sha(ROOT/fn) for fn in ['intent.schema.v1.json','router-system.txt']},'splits':all_meta,'family_counts':dict(Counter(f['split']+'/'+f['task'] for f in family_manifest)),'files':{fn:{'sha256':sha(ROOT/fn),'bytes':(ROOT/fn).stat().st_size} for fn in tracked},'token_counts':'not measured','final_policy':'Final payload and all generation/family assets excluded from portable ZIP. Parent review limited to final metadata/hashes; author may validate initial generation, never tune on final inference.'}
    (ROOT/'manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
    # Public metadata is intentionally insufficient to reconstruct final examples.
    public_manifest={k:v for k,v in manifest.items() if k!='files'}
    public_manifest['files']={fn:v for fn,v in manifest['files'].items() if fn in ['train.jsonl','dev.jsonl','train.provenance.jsonl','dev.provenance.jsonl','intent.schema.v1.json','router-system.txt','DATA_CARD.md']}
    public_manifest['sealed_final_metadata']={'total':300,'routing':210,'grounded_wisp_overviews':90,'sha256':manifest['files']['final.jsonl']['sha256'],'provenance_sha256':manifest['files']['final.provenance.jsonl']['sha256']}
    (ROOT/'upload-manifest.json').write_text(json.dumps(public_manifest,indent=2,sort_keys=True)+'\n')
    members=list(public_manifest['files'])+['upload-manifest.json']
    archive=ROOT/'ling-train-dev-20261006.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for name in sorted(members):
            info=zipfile.ZipInfo(name,date_time=(2026,10,6,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16;z.writestr(info,(ROOT/name).read_bytes())
    (ROOT/'upload-zip.json').write_text(json.dumps({'filename':archive.name,'sha256':sha(archive),'bytes':archive.stat().st_size,'members':sorted(members),'excludes':['final.jsonl','final.provenance.jsonl','families.json','generate.py','validate.py','all generation assets','private data','original heldout']},indent=2)+'\n')
    print(json.dumps({'splits':all_meta,'zip_sha256':sha(archive),'zip_bytes':archive.stat().st_size},indent=2))

if __name__=='__main__': generate()
