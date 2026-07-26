from django import template
from django.utils.html import json_script
from django.template.defaultfilters import default

register = template.Library()
from django.utils.safestring import mark_safe


@register.filter
def jsonify(value):
    return json_script(default(value))


@register.filter
def value_to_strong(value):
    return mark_safe(f"'{value}'")

@register.filter(name='add_class')
def add_class(value, arg):
    return value.as_widget(attrs={'class': arg})

@register.filter
def class_name(value):
    return value.__class__.__name__.replace('Core', '').replace('Server', ' Server')

@register.filter
def get_item(dictionary, key):
    """Get item from dictionary by key"""
    if isinstance(dictionary, dict):
        return dictionary.get(key, {})
    return {}